"""lumin-chat 主代理编排逻辑。"""

import json
import os
import platform
import sys
from pathlib import Path
from typing import Optional

from src.ai_client import AIClient
from src.config_loader import get_max_model_level, get_model_config
from src.memory_store import MemoryStore
from src.models import LLMResponse
from src.prompts import build_system_prompt
from src.session_store import SessionStore
from src.toolkit import ToolExecutor
from src.ui import TerminalUI


class LuminChatAgent:
    """负责协调 LLM、工具调用、会话状态和自动升模。"""

    def __init__(
        self,
        config: dict,
        ui: TerminalUI,
        model_level: int,
        approval_policy: str,
        workdir: Optional[str] = None,
        session_id_or_path: Optional[str] = None,
    ):
        self.config = config
        self.ui = ui
        self.model_level = model_level
        self.max_model_level = get_max_model_level(config)
        self.max_tool_rounds = int(config.get("app", {}).get("max_tool_rounds", 8))
        self.escalation_config = config.get("model_escalation", {})
        self.escalation_enabled = bool(self.escalation_config.get("enabled", True))
        self.repeat_command_threshold = int(self.escalation_config.get("repeat_command_threshold", 3))
        self.consecutive_error_threshold = int(self.escalation_config.get("consecutive_error_threshold", 4))
        self.upgrade_on_llm_error = bool(self.escalation_config.get("upgrade_on_llm_error", True))
        self.last_tool_signature = ""
        self.repeated_tool_count = 0
        self.consecutive_tool_failures = 0
        base_dir = Path(workdir or os.getcwd()).resolve()
        app_config = config.get("app", {})
        session_dir = self._resolve_state_dir(base_dir, app_config.get("session_dir", "~/.lumin-chat/sessions"))
        memory_dir = self._resolve_state_dir(base_dir, app_config.get("memory_dir", "~/.lumin-chat/memory"))
        self.session_store = SessionStore(str(session_dir))
        self.memory_store = MemoryStore(str(memory_dir))
        self.memory_recall_limit = int(app_config.get("memory_recall_limit", 5))
        self.memory_max_chars = int(app_config.get("memory_max_chars", 1600))
        self.host_context = self._collect_host_context()
        self.system_prompt = build_system_prompt(config, self.model_level, self.max_model_level)

        if session_id_or_path:
            self.session = self.session_store.load(session_id_or_path)
            self.model_level = self.session.model_level
            approval_policy = self.session.approval_policy
            base_dir = Path(self.session.cwd)
        else:
            self.session = self.session_store.create(
                model_level=self.model_level,
                approval_policy=approval_policy,
                cwd=str(base_dir),
                system_prompt=self.system_prompt,
            )
        self.memory_store.ensure_session(self.session.session_id, self.session.created_at)

        self.ai = AIClient(config=config, model_level=self.model_level)
        self.executor = ToolExecutor(
            cwd=str(base_dir),
            config=config,
            approval_policy=approval_policy,
            confirm_callback=self.ui.confirm,
        )

    @property
    def session_path(self) -> Path:
        """返回当前会话文件路径。"""

        return self.session_store.get_path(self.session.session_id)

    @property
    def cwd(self) -> str:
        """返回当前工作目录。"""

        return self.executor.cwd

    def describe_model(self) -> str:
        """返回当前模型的人类可读名称。"""

        return get_model_config(self.config, self.model_level).get("name", f"level{self.model_level}")

    def set_model_level(self, model_level: int) -> None:
        """手动切换模型级别。"""

        self.model_level = model_level
        self.ai = AIClient(config=self.config, model_level=model_level)
        self.session.model_level = model_level
        self._refresh_system_prompt()
        self._save_session()

    def set_approval_policy(self, approval_policy: str) -> None:
        """切换审批模式。"""

        self.executor.set_approval_policy(approval_policy)
        self.session.approval_policy = approval_policy
        self._save_session()

    def set_command_policy_mode(self, mode: str) -> None:
        """切换命令黑白名单模式。"""

        self.executor.set_command_policy_mode(mode)
        self.config.setdefault("command_policy", {})["mode"] = mode
        self._refresh_system_prompt()
        self._save_session()

    def reset_session(self) -> None:
        """重置当前会话。"""

        approval_policy = self.session.approval_policy
        cwd = self.executor.cwd
        self.session = self.session_store.create(
            model_level=self.model_level,
            approval_policy=approval_policy,
            cwd=cwd,
            system_prompt=self.system_prompt,
        )
        self.memory_store.ensure_session(self.session.session_id, self.session.created_at)

    def create_new_session(self) -> str:
        """新建会话，并切换到新的长期记忆空间。"""

        self.reset_session()
        self._save_session()
        return self.session.session_id

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """列出可切换的历史会话。"""

        return self.session_store.list_sessions(limit=limit)

    def switch_session(self, session_id_or_path: str) -> str:
        """切换到指定会话，并加载其长期记忆。"""

        session = self.session_store.load(session_id_or_path)
        self.session = session
        self.model_level = session.model_level
        self.ai = AIClient(config=self.config, model_level=self.model_level)
        self.executor.set_approval_policy(session.approval_policy)
        self.executor.change_directory(session.cwd)
        self._refresh_system_prompt()
        self.memory_store.ensure_session(self.session.session_id, self.session.created_at)
        self._save_session()
        return self.session.session_id

    def change_directory(self, path: str) -> str:
        """切换工作目录并保存状态。"""

        result = self.executor.change_directory(path)
        self.session.cwd = self.executor.cwd
        self._save_session()
        return result.output

    def restart_shell(self) -> None:
        """重启持久 shell。"""

        self.executor.restart_shell()
        self.session.cwd = self.executor.cwd
        self._save_session()

    def shell_state(self) -> dict:
        """返回持久 shell 状态。"""

        return self.executor.shell_state()

    def command_policy_state(self) -> dict:
        """返回当前命令策略状态。"""

        return self.executor.command_policy_state()

    def memory_state(self) -> dict:
        """返回当前会话的长期记忆概况。"""

        return self.memory_store.describe(self.session.session_id)

    def memory_summary(self, query: str = "") -> str:
        """返回当前会话的长期记忆摘要文本。"""

        text = self.memory_store.build_context(
            session_id=self.session.session_id,
            query_text=query or "最近的长期记忆",
            limit=self.memory_recall_limit,
            max_chars=self.memory_max_chars,
        )
        return text or "当前会话还没有沉淀长期记忆。"

    def run(self, user_input: str) -> str:
        """执行一次用户请求，必要时走多轮工具调用。"""

        return str(self.run_with_trace(user_input).get("content", ""))

    def run_with_trace(self, user_input: str) -> dict:
        """执行一次请求并返回结构化执行轨迹。"""

        self.session.messages.append({"role": "user", "content": user_input})
        final_content = ""
        used_tools = False
        tool_records: list[dict] = []
        recalled_memory = self.memory_store.build_context(
            session_id=self.session.session_id,
            query_text=user_input,
            limit=self.memory_recall_limit,
            max_chars=self.memory_max_chars,
        )

        for _ in range(self.max_tool_rounds):
            stream_response = bool(self.ui.show_thinking)
            response = self.ai.call(
                messages=self._build_messages_for_model(recalled_memory),
                tools=self.executor.definitions(),
                stream=stream_response,
                on_reasoning=self.ui.stream_reasoning,
                on_content=self.ui.stream_content,
            )
            if not stream_response and response.content:
                self.ui.stream_content(response.content)
            self.ui.end_stream()

            if not response.success:
                if self.upgrade_on_llm_error and self._upgrade_model(f"LLM 调用失败: {response.error or 'unknown error'}"):
                    continue
                self.ui.show_error(response.error or "LLM 调用失败")
                self._save_session()
                return {
                    "success": False,
                    "content": final_content,
                    "error": response.error or "LLM 调用失败",
                    "tool_records": tool_records,
                    "session_id": self.session.session_id,
                    "cwd": self.executor.cwd,
                }

            assistant_message = self._build_assistant_message(response)
            self.session.messages.append(assistant_message)

            if response.content:
                final_content = response.content

            if not response.tool_calls:
                if used_tools and not (response.content or "").strip():
                    final_content = self._request_final_summary() or final_content
                self.session.cwd = self.executor.cwd
                self._remember_turn(user_input, final_content)
                self._save_session()
                return {
                    "success": True,
                    "content": final_content,
                    "error": "",
                    "tool_records": tool_records,
                    "session_id": self.session.session_id,
                    "cwd": self.executor.cwd,
                }

            for tool_call in response.tool_calls:
                used_tools = True
                signature = self._tool_signature(tool_call.name, tool_call.arguments)
                if signature == self.last_tool_signature:
                    self.repeated_tool_count += 1
                else:
                    self.last_tool_signature = signature
                    self.repeated_tool_count = 1

                self.ui.show_tool_call(tool_call.name, tool_call.arguments)
                result = self.executor.execute(tool_call)
                self.ui.show_tool_result(result)
                tool_records.append(
                    {
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                        "ok": result.ok,
                        "output": result.output,
                    }
                )

                if result.ok:
                    self.consecutive_tool_failures = 0
                else:
                    self.consecutive_tool_failures += 1

                self.session.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": result.output,
                    }
                )

                if self._should_upgrade_after_tool(result.ok):
                    break

        self.ui.show_warning("达到最大工具调用轮次，已停止本次请求。")
        self.session.cwd = self.executor.cwd
        self._remember_turn(user_input, final_content)
        self._save_session()
        return {
            "success": False,
            "content": final_content,
            "error": "达到最大工具调用轮次，任务被停止。",
            "tool_records": tool_records,
            "session_id": self.session.session_id,
            "cwd": self.executor.cwd,
        }

    def _build_assistant_message(self, response: LLMResponse) -> dict:
        """把 LLM 响应转换为会话消息格式。"""

        payload = {"role": "assistant", "content": response.content or ""}
        if response.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": json.dumps(tool_call.arguments, ensure_ascii=False),
                    },
                }
                for tool_call in response.tool_calls
            ]
        return payload

    def _request_final_summary(self) -> str:
        """在工具阶段结束后请求模型给出最终总结。"""

        prompt = "你已经拿到全部工具结果。现在直接基于这些结果给用户最终答复，不要再调用工具。"
        messages = self._build_messages_for_model("") + [{"role": "system", "content": prompt}]
        response = self.ai.call(
            messages=messages,
            tools=None,
            stream=bool(self.ui.show_thinking),
            on_reasoning=self.ui.stream_reasoning,
            on_content=self.ui.stream_content,
        )
        if not self.ui.show_thinking and response.content:
            self.ui.stream_content(response.content)
        self.ui.end_stream()

        if not response.success:
            self.ui.show_error(response.error or "最终总结生成失败")
            return ""

        content = response.content.strip()
        if content:
            self.session.messages.append({"role": "assistant", "content": content})
        return content

    def _refresh_system_prompt(self) -> None:
        """按当前配置刷新系统提示词。"""

        self.system_prompt = build_system_prompt(self.config, self.model_level, self.max_model_level)
        if self.session.messages and self.session.messages[0].get("role") == "system":
            self.session.messages[0]["content"] = self.system_prompt

    def _upgrade_model(self, reason: str) -> bool:
        """在失败场景下自动升级模型级别。"""

        if not self.escalation_enabled:
            return False
        if self.model_level >= self.max_model_level:
            return False
        previous_level = self.model_level
        self.model_level += 1
        self.ai = AIClient(config=self.config, model_level=self.model_level)
        self.session.model_level = self.model_level
        self._refresh_system_prompt()
        self.last_tool_signature = ""
        self.repeated_tool_count = 0
        self.consecutive_tool_failures = 0
        self.session.messages.append(
            {
                "role": "system",
                "content": (
                    f"系统已将模型从 level {previous_level} 自动升级到 level {self.model_level}。"
                    f"原因: {reason}。请重新规划方案，避免重复失败或重复生成同一条命令。"
                ),
            }
        )
        self.ui.show_warning(f"模型已自动升级到 {self.describe_model()}，原因: {reason}")
        self._save_session()
        return True

    def _should_upgrade_after_tool(self, result_ok: bool) -> bool:
        """根据工具执行结果判断是否应自动升模。"""

        if self.repeated_tool_count >= self.repeat_command_threshold:
            return self._upgrade_model(f"同一条工具调用连续出现 {self.repeated_tool_count} 次")
        if not result_ok and self.consecutive_tool_failures >= self.consecutive_error_threshold:
            return self._upgrade_model(f"工具连续失败 {self.consecutive_tool_failures} 次")
        return False

    @staticmethod
    def _tool_signature(name: str, arguments: dict) -> str:
        """生成工具调用签名，用于重复检测。"""

        return f"{name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"

    @staticmethod
    def _resolve_state_dir(base_dir: Path, configured_path: str) -> Path:
        """解析会话和长期记忆等状态目录。"""

        expanded = Path(configured_path).expanduser()
        if expanded.is_absolute():
            return expanded
        return (base_dir / expanded).resolve()

    def _build_messages_for_model(self, recalled_memory: str) -> list[dict]:
        """构造发给模型的消息列表，并在需要时注入长期记忆。"""

        messages = list(self.session.messages)
        runtime_context = self._build_runtime_context_message()
        if runtime_context:
            messages.append({"role": "system", "content": runtime_context})
        if recalled_memory:
            messages.append({"role": "system", "content": recalled_memory})
        return messages

    def _build_runtime_context_message(self) -> str:
        """构造随请求注入的主机运行时上下文。"""

        parts = [self.host_context.strip()] if self.host_context else []
        parts.append(f"当前工作目录: {self.executor.cwd}")
        return "\n".join(part for part in parts if part).strip()

    def _collect_host_context(self) -> str:
        """收集当前主机的基础环境信息。"""

        os_release = ""
        os_release_path = Path("/etc/os-release")
        if os_release_path.exists():
            os_release = os_release_path.read_text(encoding="utf-8", errors="replace").strip()
        uname = " ".join(platform.uname())
        python_version = sys.version.split()[0]
        parts = [
            "当前主机基础信息:",
            f"- Python: {python_version}",
            f"- uname -a: {uname}",
        ]
        if os_release:
            parts.append("- /etc/os-release:")
            parts.append(os_release)
        return "\n".join(parts)

    def _remember_turn(self, user_input: str, final_content: str) -> None:
        """把本轮输入输出沉淀到会话长期记忆。"""

        if not (user_input or final_content):
            return
        self.memory_store.record_turn(self.session.session_id, user_input, final_content)

    def _save_session(self) -> None:
        """保存当前会话。"""

        self.session.cwd = self.executor.cwd
        self.session_store.save(self.session)
