"""lumin-chat 工具执行器。

这个模块负责本地工具注册、命令策略校验、Linux 持久 shell，
以及远程知识库的访问封装。
"""

import fnmatch
import getpass
import json
import os
import platform
import re
import shlex
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from src.document_library import KnowledgeBaseClient
from src.models import ToolCall, ToolExecutionResult

if os.name != "nt":
    import pty
    import select
else:
    pty = None
    select = None


ApproveCallback = Callable[[str, str], bool]


class PersistentShellSession:
    """维护 Linux PTY 持久 shell，会话内保留 cwd 和环境变量。"""

    def __init__(self, cwd: str):
        self.cwd = str(Path(cwd).resolve())
        self.shell_path = os.getenv("SHELL") or "/bin/bash"
        self.process: Optional[subprocess.Popen] = None
        self.master_fd: Optional[int] = None
        self.enabled = os.name != "nt" and Path(self.shell_path).exists()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None and self.master_fd is not None

    def ensure_started(self) -> bool:
        """按需启动持久 shell。"""

        if not self.enabled:
            return False
        if self.running:
            return True

        master_fd, slave_fd = pty.openpty()
        env = os.environ.copy()
        env["TERM"] = env.get("TERM", "xterm")
        env["PS1"] = ""
        env.pop("PROMPT_COMMAND", None)

        self.process = subprocess.Popen(
            [self.shell_path, "--noprofile", "--norc", "-i"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=self.cwd,
            env=env,
            close_fds=True,
        )
        os.close(slave_fd)
        self.master_fd = master_fd
        os.set_blocking(master_fd, False)

        ready_token = uuid.uuid4().hex
        self._send(
            "unset PROMPT_COMMAND\n"
            "export PS1=''\n"
            "stty -echo\n"
            f"printf '\n__LUMIN_CHAT_READY__{ready_token}\\x1f%s\\n' \"$PWD\"\n"
        )
        _, ready_cwd = self._read_until_ready(ready_token, timeout_seconds=10)
        if ready_cwd:
            self.cwd = ready_cwd
        self._drain()
        return True

    def restart(self, cwd: Optional[str] = None) -> None:
        """重启持久 shell，并可选择切换工作目录。"""

        self.close()
        if cwd:
            self.cwd = str(Path(cwd).resolve())

    def close(self) -> None:
        """关闭持久 shell 及其 PTY 句柄。"""

        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def run(self, command: str, timeout_seconds: int) -> tuple[int, str, str]:
        """在持久 shell 中执行命令并返回退出码、输出和当前目录。"""

        if not self.ensure_started() or self.master_fd is None:
            raise RuntimeError("persistent shell unavailable")

        marker = f"__LUMIN_CHAT_DONE__{uuid.uuid4().hex}"
        self._send(
            f"{command}\n"
            f"printf '\n{marker}\\x1f%s\\x1f%s\\n' \"$?\" \"$PWD\"\n"
        )
        output, exit_code, current_cwd = self._read_until_marker(marker, timeout_seconds)
        self.cwd = current_cwd or self.cwd
        return exit_code, output, self.cwd

    def describe(self) -> Dict[str, object]:
        """返回持久 shell 当前状态，供 UI 和调试输出使用。"""

        return {
            "enabled": self.enabled,
            "running": self.running,
            "shell_path": self.shell_path,
            "cwd": self.cwd,
        }

    def _send(self, text: str) -> None:
        if self.master_fd is None:
            raise RuntimeError("shell is not started")
        os.write(self.master_fd, text.encode("utf-8"))

    def _read_until_ready(self, token: str, timeout_seconds: int) -> tuple[str, str]:
        """等待 shell 启动完成标记。"""

        marker = f"__LUMIN_CHAT_READY__{token}"
        deadline = time.time() + timeout_seconds
        buffer = ""
        regex = re.compile(rf"{re.escape(marker)}\x1f(?P<pwd>.*?)\r?\n", re.DOTALL)
        while time.time() < deadline:
            buffer += self._read_available(deadline)
            match = regex.search(buffer)
            if match:
                return buffer[: match.start()], match.group("pwd")
        self.restart(self.cwd)
        raise TimeoutError("persistent shell startup timeout")

    def _read_until_marker(self, marker: str, timeout_seconds: int) -> tuple[str, int, str]:
        """等待命令执行完成标记。"""

        deadline = time.time() + timeout_seconds
        buffer = ""
        regex = re.compile(rf"\r?\n{re.escape(marker)}\x1f(?P<code>-?\d+)\x1f(?P<pwd>.*?)\r?\n", re.DOTALL)

        while time.time() < deadline:
            buffer += self._read_available(deadline)
            match = regex.search(buffer)
            if match:
                output = buffer[: match.start()]
                exit_code = int(match.group("code"))
                pwd = match.group("pwd")
                return output.strip(), exit_code, pwd
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError("persistent shell exited unexpectedly")

        self.restart(self.cwd)
        raise TimeoutError(f"persistent shell command timeout after {timeout_seconds}s")

    def _read_available(self, deadline: float) -> str:
        """读取当前 PTY 可用输出。"""

        if self.master_fd is None:
            return ""
        remaining = max(0.0, deadline - time.time())
        if remaining == 0.0:
            return ""
        ready, _, _ = select.select([self.master_fd], [], [], remaining)
        if not ready:
            return ""
        chunks = []
        while True:
            try:
                chunk = os.read(self.master_fd, 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            chunks.append(chunk.decode("utf-8", errors="replace"))
            if len(chunk) < 4096:
                break
        return "".join(chunks)

    def _drain(self) -> None:
        """清空 PTY 中残留的初始化输出。"""

        if self.master_fd is None:
            return
        deadline = time.time() + 0.2
        while time.time() < deadline:
            ready, _, _ = select.select([self.master_fd], [], [], 0.05)
            if not ready:
                break
            try:
                if not os.read(self.master_fd, 4096):
                    break
            except BlockingIOError:
                break


class ToolExecutor:
    """统一管理 lumin-chat 的工具定义和执行流程。"""

    def __init__(
        self,
        cwd: str,
        config: Dict,
        approval_policy: str = "auto",
        confirm_callback: Optional[ApproveCallback] = None,
    ):
        self.config = config
        self.cwd = str(Path(cwd).resolve())
        self.approval_policy = approval_policy
        self.confirm_callback = confirm_callback
        self.shell_session = PersistentShellSession(self.cwd)
        self.command_policy = config.get("command_policy", {})
        self.command_policy_mode = self.command_policy.get("mode", "blacklist")
        self.blacklist = [item.lower() for item in self.command_policy.get("blacklist", [])]
        self.whitelist = [item.lower() for item in self.command_policy.get("whitelist", [])]
        self.knowledge_base = KnowledgeBaseClient(config)

    def definitions(self) -> List[Dict]:
        """返回提供给大模型的工具定义列表。"""

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "run_shell_command",
                    "description": "Run a shell command in the current Linux terminal session.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "Shell command to execute."},
                            "timeout_seconds": {"type": "integer", "description": "Timeout in seconds.", "default": 30},
                            "cwd": {"type": "string", "description": "Optional working directory override."},
                        },
                        "required": ["command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "change_directory",
                    "description": "Change the current working directory for later tool calls.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Directory path to switch to."}
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_directory",
                    "description": "List files and directories from a path.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Directory path to inspect."},
                            "recursive": {"type": "boolean", "description": "Whether to recurse.", "default": False},
                            "max_entries": {"type": "integer", "description": "Maximum number of items.", "default": 200},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_text",
                    "description": "Search text inside files under a directory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string", "description": "Text to search for."},
                            "path": {"type": "string", "description": "Directory or file path to search."},
                            "glob": {"type": "string", "description": "Glob pattern for files.", "default": "*"},
                            "case_sensitive": {"type": "boolean", "description": "Case sensitive search.", "default": False},
                            "max_matches": {"type": "integer", "description": "Maximum number of matches.", "default": 50},
                        },
                        "required": ["pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read lines from a text file.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path to read."},
                            "start_line": {"type": "integer", "description": "1-based starting line.", "default": 1},
                            "end_line": {"type": "integer", "description": "1-based ending line.", "default": 200},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Write text content to a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path to write."},
                            "content": {"type": "string", "description": "Text content to write."},
                            "append": {"type": "boolean", "description": "Append instead of overwrite.", "default": False},
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_environment",
                    "description": "Get current runtime environment details.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
        ]
        if self.knowledge_base.enabled:
            tools.extend(
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "list_knowledge_documents",
                            "description": "List markdown and text documents in the configured remote knowledge base.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "keyword": {"type": "string", "description": "Optional keyword to filter file names."},
                                    "limit": {"type": "integer", "description": "Maximum number of document names.", "default": 50},
                                },
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "read_knowledge_document",
                            "description": "Read lines from a specific document in the remote knowledge base.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": "Relative path from the knowledge base root."},
                                    "start_line": {"type": "integer", "description": "1-based starting line.", "default": 1},
                                    "end_line": {"type": "integer", "description": "1-based ending line.", "default": 200},
                                },
                                "required": ["path"],
                            },
                        },
                    },
                ]
            )
        return tools

    def execute(self, tool_call: ToolCall) -> ToolExecutionResult:
        """执行单个工具调用，并兼容退化的 raw 参数格式。"""

        name = tool_call.name
        arguments = self._normalize_tool_arguments(name, tool_call.arguments)
        if name == "run_shell_command":
            return self.run_shell_command(**arguments)
        if name == "change_directory":
            return self.change_directory(**arguments)
        if name == "list_directory":
            return self.list_directory(**arguments)
        if name == "search_text":
            return self.search_text(**arguments)
        if name == "read_file":
            return self.read_file(**arguments)
        if name == "write_file":
            return self.write_file(**arguments)
        if name == "get_environment":
            return self.get_environment()
        if name == "list_knowledge_documents":
            return self.list_knowledge_documents(**arguments)
        if name == "read_knowledge_document":
            return self.read_knowledge_document(**arguments)
        return ToolExecutionResult(name=name, ok=False, output=f"Unknown tool: {name}")

    def set_approval_policy(self, approval_policy: str) -> None:
        """更新工具审批策略。"""

        self.approval_policy = approval_policy

    def set_command_policy_mode(self, mode: str) -> None:
        """切换黑名单或白名单模式。"""

        if mode not in {"blacklist", "whitelist"}:
            raise ValueError(f"未知命令策略模式: {mode}")
        self.command_policy_mode = mode

    def restart_shell(self) -> None:
        """重启持久 shell。"""

        self.shell_session.restart(self.cwd)

    def shell_state(self) -> Dict[str, object]:
        """返回持久 shell 的可观测状态。"""

        state = self.shell_session.describe()
        state["cwd"] = self.cwd
        return state

    def command_policy_state(self) -> Dict[str, object]:
        """返回当前命令策略状态。"""

        return {
            "mode": self.command_policy_mode,
            "blacklist": self.blacklist,
            "whitelist": self.whitelist,
        }

    def run_shell_command(
        self,
        command: str,
        timeout_seconds: int = 30,
        cwd: Optional[str] = None,
    ) -> ToolExecutionResult:
        """执行 shell 命令，优先复用 Linux 持久 shell。"""

        allowed, policy_reason = self._check_command_policy(command)
        if not allowed:
            return ToolExecutionResult(name="run_shell_command", ok=False, output=policy_reason, metadata={"blocked": True})

        allowed, reason = self._check_approval("run_shell_command", command)
        if not allowed:
            return ToolExecutionResult(name="run_shell_command", ok=False, output=reason)

        exec_cwd = self._resolve_path(cwd) if cwd else self.cwd
        if not Path(exec_cwd).exists():
            return ToolExecutionResult(name="run_shell_command", ok=False, output=f"目录不存在: {exec_cwd}")

        try:
            if self.shell_session.enabled and cwd is None:
                exit_code, stdout, updated_cwd = self.shell_session.run(command, timeout_seconds)
                self.cwd = updated_cwd
                payload = {
                    "command": command,
                    "cwd": updated_cwd,
                    "exit_code": exit_code,
                    "stdout": self._truncate(stdout),
                    "stderr": "",
                    "persistent_shell": True,
                }
                return ToolExecutionResult(
                    name="run_shell_command",
                    ok=exit_code == 0,
                    output=json.dumps(payload, ensure_ascii=False, indent=2),
                    metadata={"exit_code": exit_code, "cwd": updated_cwd, "persistent_shell": True},
                )

            if os.name == "nt":
                args = ["powershell", "-NoProfile", "-Command", command]
            else:
                args = ["/bin/bash", "-lc", command]

            completed = subprocess.run(
                args,
                cwd=exec_cwd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            payload = {
                "command": command,
                "cwd": exec_cwd,
                "exit_code": completed.returncode,
                "stdout": self._truncate(completed.stdout),
                "stderr": self._truncate(completed.stderr),
                "persistent_shell": False,
            }
            return ToolExecutionResult(
                name="run_shell_command",
                ok=completed.returncode == 0,
                output=json.dumps(payload, ensure_ascii=False, indent=2),
                metadata={"exit_code": completed.returncode, "cwd": exec_cwd, "persistent_shell": False},
            )
        except subprocess.TimeoutExpired:
            return ToolExecutionResult(name="run_shell_command", ok=False, output=f"命令超时: {timeout_seconds}s")
        except Exception as exc:
            return ToolExecutionResult(name="run_shell_command", ok=False, output=f"命令执行失败: {exc}")

    def change_directory(self, path: str) -> ToolExecutionResult:
        """切换当前工作目录，并同步持久 shell 状态。"""

        target = self._resolve_path(path)
        target_path = Path(target)
        if not target_path.exists():
            return ToolExecutionResult(name="change_directory", ok=False, output=f"目录不存在: {target}")
        if not target_path.is_dir():
            return ToolExecutionResult(name="change_directory", ok=False, output=f"不是目录: {target}")
        self.cwd = str(target_path)
        self.shell_session.cwd = self.cwd
        if self.shell_session.running:
            command = f"cd {shlex.quote(self.cwd)}"
            try:
                self.shell_session.run(command, timeout_seconds=10)
            except Exception:
                self.shell_session.restart(self.cwd)
        return ToolExecutionResult(name="change_directory", ok=True, output=f"当前目录已切换到 {self.cwd}", metadata={"cwd": self.cwd})

    def list_directory(self, path: str = ".", recursive: bool = False, max_entries: int = 200) -> ToolExecutionResult:
        """列出目录内容。"""

        target = Path(self._resolve_path(path))
        if not target.exists():
            return ToolExecutionResult(name="list_directory", ok=False, output=f"路径不存在: {target}")

        entries = []
        iterator = target.rglob("*") if recursive else target.iterdir()
        for index, item in enumerate(iterator):
            if index >= max_entries:
                break
            entries.append(
                {
                    "path": str(item),
                    "type": "dir" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else None,
                }
            )
        return ToolExecutionResult(name="list_directory", ok=True, output=json.dumps(entries, ensure_ascii=False, indent=2))

    def search_text(
        self,
        pattern: str,
        path: str = ".",
        glob: str = "*",
        case_sensitive: bool = False,
        max_matches: int = 50,
    ) -> ToolExecutionResult:
        """在文件中搜索文本内容。"""

        target = Path(self._resolve_path(path))
        if not target.exists():
            return ToolExecutionResult(name="search_text", ok=False, output=f"路径不存在: {target}")

        matches = []
        files = [target] if target.is_file() else target.rglob("*")
        needle = pattern if case_sensitive else pattern.lower()

        for file_path in files:
            if len(matches) >= max_matches or not file_path.is_file():
                if len(matches) >= max_matches:
                    break
                continue
            if not fnmatch.fnmatch(file_path.name, glob):
                continue
            if file_path.stat().st_size > 2 * 1024 * 1024:
                continue
            try:
                with file_path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        haystack = line if case_sensitive else line.lower()
                        if needle in haystack:
                            matches.append(
                                {
                                    "path": str(file_path),
                                    "line": line_number,
                                    "content": line.rstrip(),
                                }
                            )
                            if len(matches) >= max_matches:
                                break
            except OSError:
                continue

        return ToolExecutionResult(name="search_text", ok=True, output=json.dumps(matches, ensure_ascii=False, indent=2))

    def read_file(self, path: str, start_line: int = 1, end_line: int = 200) -> ToolExecutionResult:
        """读取文本文件指定行范围。"""

        target = Path(self._resolve_path(path))
        if not target.exists():
            return ToolExecutionResult(name="read_file", ok=False, output=f"文件不存在: {target}")
        if not target.is_file():
            return ToolExecutionResult(name="read_file", ok=False, output=f"不是文件: {target}")

        start_line = max(1, start_line)
        end_line = max(start_line, end_line)

        with target.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[start_line - 1 : end_line]

        numbered = [f"{index}: {line.rstrip()}" for index, line in enumerate(lines, start=start_line)]
        return ToolExecutionResult(name="read_file", ok=True, output="\n".join(numbered))

    def write_file(self, path: str, content: str, append: bool = False) -> ToolExecutionResult:
        """写入或追加文件内容。"""

        allowed, reason = self._check_approval("write_file", path)
        if not allowed:
            return ToolExecutionResult(name="write_file", ok=False, output=reason)

        target = Path(self._resolve_path(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with target.open(mode, encoding="utf-8") as handle:
            handle.write(content)
        action = "追加" if append else "写入"
        return ToolExecutionResult(name="write_file", ok=True, output=f"已{action}文件: {target}", metadata={"path": str(target)})

    def list_knowledge_documents(self, keyword: str = "", limit: int = 50) -> ToolExecutionResult:
        """列出远程知识库中的候选文档。"""

        try:
            payload = self.knowledge_base.list_documents(keyword=keyword, limit=limit)
            return ToolExecutionResult(name="list_knowledge_documents", ok=True, output=json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:
            return ToolExecutionResult(name="list_knowledge_documents", ok=False, output=f"文档库访问失败: {exc}")

    def read_knowledge_document(self, path: str, start_line: int = 1, end_line: int = 200) -> ToolExecutionResult:
        """读取远程知识库文档。"""

        try:
            payload = self.knowledge_base.read_document(path=path, start_line=start_line, end_line=end_line)
            return ToolExecutionResult(name="read_knowledge_document", ok=True, output=payload)
        except Exception as exc:
            return ToolExecutionResult(name="read_knowledge_document", ok=False, output=f"文档读取失败: {exc}")

    def get_environment(self) -> ToolExecutionResult:
        """汇总当前运行环境、shell 和策略信息。"""

        payload = {
            "cwd": self.cwd,
            "os": platform.system(),
            "release": platform.release(),
            "python_version": sys.version,
            "user": getpass.getuser(),
            "hostname": socket.gethostname(),
            "shell": os.getenv("SHELL") or os.getenv("COMSPEC") or "unknown",
            "persistent_shell": self.shell_state(),
            "command_policy": self.command_policy_state(),
            "knowledge_base": self.knowledge_base.describe(),
        }
        return ToolExecutionResult(name="get_environment", ok=True, output=json.dumps(payload, ensure_ascii=False, indent=2))

    def _resolve_path(self, raw_path: str) -> str:
        """将相对路径解析为基于当前 cwd 的绝对路径。"""

        expanded = os.path.expanduser(raw_path)
        candidate = Path(expanded)
        if candidate.is_absolute():
            return str(candidate.resolve())
        return str((Path(self.cwd) / candidate).resolve())

    def _normalize_tool_arguments(self, tool_name: str, arguments: object) -> Dict[str, object]:
        """标准化模型生成的工具参数，兼容 raw 包装和多余字段。"""

        if isinstance(arguments, dict):
            normalized = dict(arguments)
        elif isinstance(arguments, str):
            normalized = self._coerce_raw_tool_arguments(tool_name, arguments)
        else:
            normalized = {}

        if set(normalized.keys()) == {"raw"}:
            normalized = self._coerce_raw_tool_arguments(tool_name, normalized.get("raw"))

        if "raw" in normalized:
            raw_value = normalized.pop("raw")
            fallback = self._coerce_raw_tool_arguments(tool_name, raw_value)
            for key, value in fallback.items():
                normalized.setdefault(key, value)

        allowed_keys = {
            "run_shell_command": {"command", "timeout_seconds", "cwd"},
            "change_directory": {"path"},
            "list_directory": {"path", "recursive", "max_entries"},
            "search_text": {"pattern", "path", "glob", "case_sensitive", "max_matches"},
            "read_file": {"path", "start_line", "end_line"},
            "write_file": {"path", "content", "append"},
            "get_environment": set(),
            "list_knowledge_documents": {"keyword", "limit"},
            "read_knowledge_document": {"path", "start_line", "end_line"},
        }
        valid_keys = allowed_keys.get(tool_name)
        if valid_keys is None:
            return normalized
        return {key: value for key, value in normalized.items() if key in valid_keys}

    def _coerce_raw_tool_arguments(self, tool_name: str, raw_value: object) -> Dict[str, object]:
        """将 raw 字符串尽量还原为目标工具需要的结构化参数。"""

        if isinstance(raw_value, dict):
            return self._normalize_tool_arguments(tool_name, raw_value)
        if not isinstance(raw_value, str):
            return {}

        text = raw_value.strip()
        if not text:
            return {}

        if text.startswith("{") and text.endswith("}"):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                return self._normalize_tool_arguments(tool_name, decoded)

        primary_field = {
            "run_shell_command": "command",
            "change_directory": "path",
            "read_file": "path",
            "write_file": "content",
            "read_knowledge_document": "path",
        }.get(tool_name)
        if primary_field is None:
            return {}
        return {primary_field: text}

    def _check_command_policy(self, command: str) -> tuple[bool, str]:
        """按黑名单或白名单策略校验命令。"""

        normalized = command.lower().strip()
        if not normalized:
            return False, "空命令不允许执行"

        if self.command_policy_mode == "blacklist":
            for pattern in self.blacklist:
                if pattern and pattern in normalized:
                    return False, f"命令被黑名单策略阻止: {pattern}"
            return True, "allowed"

        if self.command_policy_mode == "whitelist":
            segments = [segment.strip() for segment in re.split(r"(?:&&|\|\||;|\|)", command) if segment.strip()]
            if not segments:
                return False, "空命令不允许执行"
            for segment in segments:
                token = self._extract_command_token(segment)
                if token not in self.whitelist:
                    return False, f"命令不在白名单中: {token}"
            return True, "allowed"

        return False, f"未知命令策略模式: {self.command_policy_mode}"

    def _check_approval(self, action: str, details: str) -> tuple[bool, str]:
        """按审批模式决定是否允许执行敏感操作。"""

        if self.approval_policy == "auto":
            return True, "approved"
        if self.approval_policy == "read-only":
            return False, f"审批策略 {self.approval_policy} 禁止执行 {action}"
        if self.confirm_callback is None:
            return False, f"审批策略 {self.approval_policy} 需要交互确认，但当前模式不可确认"
        approved = self.confirm_callback(action, details)
        if not approved:
            return False, f"用户拒绝执行 {action}: {details}"
        return True, "approved"

    @staticmethod
    def _extract_command_token(command: str) -> str:
        """提取命令段的主执行文件名。"""

        try:
            tokens = shlex.split(command, posix=os.name != "nt")
        except ValueError:
            tokens = command.strip().split()
        if not tokens:
            return ""
        return Path(tokens[0]).name.lower()

    @staticmethod
    def _truncate(text: str, limit: int = 12000) -> str:
        """限制命令输出长度，避免上下文膨胀。"""

        if len(text) <= limit:
            return text
        return text[:limit] + "\n...<truncated>..."
