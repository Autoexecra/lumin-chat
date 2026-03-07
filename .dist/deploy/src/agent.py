import json
import os
from pathlib import Path
from typing import Optional

from src.ai_client import AIClient
from src.config_loader import get_model_config
from src.models import LLMResponse, SessionState, ToolCall
from src.prompts import build_system_prompt
from src.session_store import SessionStore
from src.toolkit import ToolExecutor
from src.ui import TerminalUI


class CopilotTerminalAgent:
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
        self.max_tool_rounds = int(config.get("app", {}).get("max_tool_rounds", 8))
        base_dir = Path(workdir or os.getcwd()).resolve()
        session_dir = base_dir / config.get("app", {}).get("session_dir", ".copilot-terminal/sessions")
        self.session_store = SessionStore(str(session_dir))
        self.system_prompt = build_system_prompt()

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

        self.ai = AIClient(config=config, model_level=self.model_level)
        self.executor = ToolExecutor(
            cwd=str(base_dir),
            approval_policy=approval_policy,
            confirm_callback=self.ui.confirm,
        )

    @property
    def session_path(self) -> Path:
        return self.session_store.get_path(self.session.session_id)

    @property
    def cwd(self) -> str:
        return self.executor.cwd

    def describe_model(self) -> str:
        return get_model_config(self.config, self.model_level).get("name", f"level{self.model_level}")

    def set_model_level(self, model_level: int) -> None:
        self.model_level = model_level
        self.ai = AIClient(config=self.config, model_level=model_level)
        self.session.model_level = model_level
        self._save_session()

    def set_approval_policy(self, approval_policy: str) -> None:
        self.executor.set_approval_policy(approval_policy)
        self.session.approval_policy = approval_policy
        self._save_session()

    def reset_session(self) -> None:
        approval_policy = self.session.approval_policy
        cwd = self.executor.cwd
        self.session = self.session_store.create(
            model_level=self.model_level,
            approval_policy=approval_policy,
            cwd=cwd,
            system_prompt=self.system_prompt,
        )

    def change_directory(self, path: str) -> str:
        result = self.executor.change_directory(path)
        self.session.cwd = self.executor.cwd
        self._save_session()
        return result.output

    def restart_shell(self) -> None:
        self.executor.restart_shell()
        self.session.cwd = self.executor.cwd
        self._save_session()

    def shell_state(self) -> dict:
        return self.executor.shell_state()

    def run(self, user_input: str) -> str:
        self.session.messages.append({"role": "user", "content": user_input})
        final_content = ""
        used_tools = False

        for _ in range(self.max_tool_rounds):
            response = self.ai.call(
                messages=self.session.messages,
                tools=self.executor.definitions(),
                stream=True,
                on_reasoning=self.ui.stream_reasoning,
                on_content=self.ui.stream_content,
            )
            self.ui.end_stream()

            if not response.success:
                self.ui.show_error(response.error or "LLM 调用失败")
                self._save_session()
                return ""

            assistant_message = self._build_assistant_message(response)
            self.session.messages.append(assistant_message)

            if response.content:
                final_content = response.content

            if not response.tool_calls:
                if used_tools and not (response.content or "").strip():
                    final_content = self._request_final_summary() or final_content
                self.session.cwd = self.executor.cwd
                self._save_session()
                return final_content

            for tool_call in response.tool_calls:
                used_tools = True
                self.ui.show_tool_call(tool_call.name, tool_call.arguments)
                result = self.executor.execute(tool_call)
                self.ui.show_tool_result(result)
                self.session.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": result.output,
                    }
                )

        self.ui.show_warning("达到最大工具调用轮次，已停止本次请求。")
        self.session.cwd = self.executor.cwd
        self._save_session()
        return final_content

    def _build_assistant_message(self, response: LLMResponse) -> dict:
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
        prompt = "你已经拿到全部工具结果。现在直接基于这些结果给用户最终答复，不要再调用工具。"
        messages = self.session.messages + [{"role": "system", "content": prompt}]
        response = self.ai.call(
            messages=messages,
            tools=None,
            stream=True,
            on_reasoning=self.ui.stream_reasoning,
            on_content=self.ui.stream_content,
        )
        self.ui.end_stream()

        if not response.success:
            self.ui.show_error(response.error or "最终总结生成失败")
            return ""

        content = response.content.strip()
        if content:
            self.session.messages.append({"role": "assistant", "content": content})
        return content

    def _save_session(self) -> None:
        self.session.cwd = self.executor.cwd
        self.session_store.save(self.session)
