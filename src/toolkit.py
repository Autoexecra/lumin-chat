# Copyright (c) 2026 Autoexecra
# Licensed under the Apache License, Version 2.0.
# See LICENSE in the project root for license terms.

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
import shutil
import socket
import subprocess
import sys
import time
import uuid
from difflib import unified_diff
from codecs import decode as codecs_decode
from pathlib import Path
from typing import Callable, Dict, List, Optional

from src.document_library import KnowledgeBaseClient
from src.models import ToolCall, ToolExecutionResult
from src.ssh_client import SSHConnectionConfig, SSHRemoteClient
from src.web_tools import WebToolClient, format_payload

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
            "export PS2=''\n"
            "bind 'set enable-bracketed-paste off' >/dev/null 2>&1 || true\n"
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

    TOOL_ALLOWED_KEYS = {
        "run_shell_command": {"command", "timeout_seconds", "cwd"},
        "change_directory": {"path"},
        "list_directory": {"path", "recursive", "max_entries"},
        "search_text": {"pattern", "path", "glob", "case_sensitive", "max_matches"},
        "find_files": {"pattern", "path", "max_results", "include_hidden"},
        "read_file": {"path", "start_line", "end_line"},
        "write_file": {"path", "content", "append"},
        "replace_in_file": {"path", "search_text", "replace_text", "replace_all"},
        "insert_in_file": {"path", "content", "line_number"},
        "get_environment": set(),
        "get_workspace_overview": {"path", "max_depth", "max_entries"},
        "git_status": {"repo_path", "include_untracked"},
        "git_diff": {"repo_path", "pathspec", "cached", "max_chars"},
        "ssh_execute_command": {"host", "port", "username", "password", "command", "timeout_seconds", "cwd"},
        "ssh_upload_file": {"host", "port", "username", "password", "local_path", "remote_path"},
        "ssh_download_file": {"host", "port", "username", "password", "remote_path", "local_path"},
        "ssh_list_directory": {"host", "port", "username", "password", "path", "recursive", "max_entries"},
        "ssh_read_file": {"host", "port", "username", "password", "path", "start_line", "end_line"},
        "ssh_write_file": {"host", "port", "username", "password", "path", "content", "append"},
        "ssh_make_directory": {"host", "port", "username", "password", "path"},
        "ssh_remove_path": {"host", "port", "username", "password", "path"},
        "ssh_path_exists": {"host", "port", "username", "password", "path"},
        "fetch_web_page": {"url", "timeout_seconds", "max_chars"},
        "search_web": {"query", "limit", "timeout_seconds"},
        "list_knowledge_documents": {"keyword", "limit"},
        "read_knowledge_document": {"path", "start_line", "end_line"},
        "write_knowledge_document": {"path", "content", "append"},
    }

    TOOL_PRIMARY_FIELDS = {
        "run_shell_command": "command",
        "change_directory": "path",
        "find_files": "pattern",
        "read_file": "path",
        "write_file": "content",
        "replace_in_file": "search_text",
        "insert_in_file": "content",
        "git_status": "repo_path",
        "git_diff": "pathspec",
        "read_knowledge_document": "path",
        "ssh_execute_command": "command",
        "ssh_upload_file": "remote_path",
        "ssh_download_file": "remote_path",
        "ssh_list_directory": "path",
        "ssh_read_file": "path",
        "ssh_write_file": "content",
        "fetch_web_page": "url",
        "search_web": "query",
        "write_knowledge_document": "path",
    }

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
        self.web_client = WebToolClient()

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
                    "name": "find_files",
                    "description": "Find files by glob pattern, similar to rg --files plus pattern filtering.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string", "description": "Glob pattern such as src/**/*.py.", "default": "**/*"},
                            "path": {"type": "string", "description": "Base directory to search from.", "default": "."},
                            "max_results": {"type": "integer", "description": "Maximum number of results.", "default": 200},
                            "include_hidden": {"type": "boolean", "description": "Whether to include dotfiles and dot-directories.", "default": False}
                        }
                    }
                }
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
                    "name": "replace_in_file",
                    "description": "Precisely replace text in a file without rewriting unrelated content.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path to edit."},
                            "search_text": {"type": "string", "description": "Exact text to find."},
                            "replace_text": {"type": "string", "description": "Replacement text."},
                            "replace_all": {"type": "boolean", "description": "Replace all occurrences instead of exactly one.", "default": False}
                        },
                        "required": ["path", "search_text", "replace_text"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "insert_in_file",
                    "description": "Insert text into a file at a specific 1-based line number.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path to edit."},
                            "content": {"type": "string", "description": "Text to insert."},
                            "line_number": {"type": "integer", "description": "1-based insertion line. len+1 appends to the end.", "default": 1}
                        },
                        "required": ["path", "content"]
                    }
                }
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
            {
                "type": "function",
                "function": {
                    "name": "get_workspace_overview",
                    "description": "Summarize the current workspace structure, git state and major file types.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Workspace path to inspect.", "default": "."},
                            "max_depth": {"type": "integer", "description": "Maximum directory depth to summarize.", "default": 2},
                            "max_entries": {"type": "integer", "description": "Maximum number of files to sample.", "default": 80}
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "git_status",
                    "description": "Read the current git branch and changed files for the workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "repo_path": {"type": "string", "description": "Path inside the git repository.", "default": "."},
                            "include_untracked": {"type": "boolean", "description": "Whether to include untracked files.", "default": True}
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "git_diff",
                    "description": "Read git diff text for unstaged or staged changes.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "repo_path": {"type": "string", "description": "Path inside the git repository.", "default": "."},
                            "pathspec": {"type": "string", "description": "Optional file or glob pathspec to limit the diff."},
                            "cached": {"type": "boolean", "description": "Read staged diff instead of unstaged diff.", "default": False},
                            "max_chars": {"type": "integer", "description": "Maximum diff characters to return.", "default": 12000}
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "ssh_execute_command",
                    "description": "Run a shell command on a remote host over SSH and return exit code, stdout and stderr.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string", "description": "Remote host IP or domain."},
                            "port": {"type": "integer", "description": "SSH port.", "default": 22},
                            "username": {"type": "string", "description": "SSH username."},
                            "password": {"type": "string", "description": "SSH password. Leave empty for key-based login."},
                            "command": {"type": "string", "description": "Shell command to execute remotely."},
                            "timeout_seconds": {"type": "integer", "description": "Timeout in seconds.", "default": 60},
                            "cwd": {"type": "string", "description": "Optional remote working directory."}
                        },
                        "required": ["host", "username", "command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ssh_upload_file",
                    "description": "Upload a local file to a remote host over SSH/SFTP.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string", "description": "Remote host IP or domain."},
                            "port": {"type": "integer", "description": "SSH port.", "default": 22},
                            "username": {"type": "string", "description": "SSH username."},
                            "password": {"type": "string", "description": "SSH password. Leave empty for key-based login."},
                            "local_path": {"type": "string", "description": "Local file path."},
                            "remote_path": {"type": "string", "description": "Remote destination file path."},
                        },
                        "required": ["host", "username", "local_path", "remote_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ssh_download_file",
                    "description": "Download a file from a remote host over SSH/SFTP.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string", "description": "Remote host IP or domain."},
                            "port": {"type": "integer", "description": "SSH port.", "default": 22},
                            "username": {"type": "string", "description": "SSH username."},
                            "password": {"type": "string", "description": "SSH password. Leave empty for key-based login."},
                            "remote_path": {"type": "string", "description": "Remote source file path."},
                            "local_path": {"type": "string", "description": "Local destination file path."},
                        },
                        "required": ["host", "username", "remote_path", "local_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ssh_list_directory",
                    "description": "List files and directories on a remote host over SSH/SFTP.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string", "description": "Remote host IP or domain."},
                            "port": {"type": "integer", "description": "SSH port.", "default": 22},
                            "username": {"type": "string", "description": "SSH username."},
                            "password": {"type": "string", "description": "SSH password. Leave empty for key-based login."},
                            "path": {"type": "string", "description": "Remote directory path."},
                            "recursive": {"type": "boolean", "description": "Whether to recurse.", "default": False},
                            "max_entries": {"type": "integer", "description": "Maximum number of items.", "default": 200},
                        },
                        "required": ["host", "username", "path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ssh_read_file",
                    "description": "Read a text file from a remote host over SSH/SFTP.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string", "description": "Remote host IP or domain."},
                            "port": {"type": "integer", "description": "SSH port.", "default": 22},
                            "username": {"type": "string", "description": "SSH username."},
                            "password": {"type": "string", "description": "SSH password. Leave empty for key-based login."},
                            "path": {"type": "string", "description": "Remote text file path."},
                            "start_line": {"type": "integer", "description": "1-based starting line.", "default": 1},
                            "end_line": {"type": "integer", "description": "1-based ending line.", "default": 200},
                        },
                        "required": ["host", "username", "path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ssh_write_file",
                    "description": "Write a text file on a remote host over SSH/SFTP.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string", "description": "Remote host IP or domain."},
                            "port": {"type": "integer", "description": "SSH port.", "default": 22},
                            "username": {"type": "string", "description": "SSH username."},
                            "password": {"type": "string", "description": "SSH password. Leave empty for key-based login."},
                            "path": {"type": "string", "description": "Remote text file path."},
                            "content": {"type": "string", "description": "Text content to write."},
                            "append": {"type": "boolean", "description": "Append instead of overwrite.", "default": False},
                        },
                        "required": ["host", "username", "path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ssh_make_directory",
                    "description": "Create a directory on a remote host over SSH/SFTP.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string", "description": "Remote host IP or domain."},
                            "port": {"type": "integer", "description": "SSH port.", "default": 22},
                            "username": {"type": "string", "description": "SSH username."},
                            "password": {"type": "string", "description": "SSH password. Leave empty for key-based login."},
                            "path": {"type": "string", "description": "Remote directory path."}
                        },
                        "required": ["host", "username", "path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "ssh_remove_path",
                    "description": "Remove a remote file or directory over SSH/SFTP.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string", "description": "Remote host IP or domain."},
                            "port": {"type": "integer", "description": "SSH port.", "default": 22},
                            "username": {"type": "string", "description": "SSH username."},
                            "password": {"type": "string", "description": "SSH password. Leave empty for key-based login."},
                            "path": {"type": "string", "description": "Remote file or directory path."}
                        },
                        "required": ["host", "username", "path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "ssh_path_exists",
                    "description": "Check whether a remote path exists over SSH/SFTP.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string", "description": "Remote host IP or domain."},
                            "port": {"type": "integer", "description": "SSH port.", "default": 22},
                            "username": {"type": "string", "description": "SSH username."},
                            "password": {"type": "string", "description": "SSH password. Leave empty for key-based login."},
                            "path": {"type": "string", "description": "Remote file or directory path."}
                        },
                        "required": ["host", "username", "path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_web_page",
                    "description": "Fetch a web page, extract the title and readable text content.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "Target URL."},
                            "timeout_seconds": {"type": "integer", "description": "Timeout in seconds.", "default": 60},
                            "max_chars": {"type": "integer", "description": "Maximum extracted text length.", "default": 12000},
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_web",
                    "description": "Search the public web and return titles, links and snippets.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query."},
                            "limit": {"type": "integer", "description": "Maximum results.", "default": 5},
                            "timeout_seconds": {"type": "integer", "description": "Timeout in seconds.", "default": 60},
                        },
                        "required": ["query"],
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
                    {
                        "type": "function",
                        "function": {
                            "name": "write_knowledge_document",
                            "description": "Write a markdown or text document back into the configured remote knowledge base.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": "Relative path from the knowledge base root."},
                                    "content": {"type": "string", "description": "Document text to write."},
                                    "append": {"type": "boolean", "description": "Append instead of overwrite.", "default": False},
                                },
                                "required": ["path", "content"],
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
        try:
            if name == "run_shell_command":
                return self.run_shell_command(**arguments)
            if name == "change_directory":
                return self.change_directory(**arguments)
            if name == "list_directory":
                return self.list_directory(**arguments)
            if name == "search_text":
                return self.search_text(**arguments)
            if name == "find_files":
                return self.find_files(**arguments)
            if name == "read_file":
                return self.read_file(**arguments)
            if name == "write_file":
                return self.write_file(**arguments)
            if name == "replace_in_file":
                return self.replace_in_file(**arguments)
            if name == "insert_in_file":
                return self.insert_in_file(**arguments)
            if name == "get_environment":
                return self.get_environment()
            if name == "get_workspace_overview":
                return self.get_workspace_overview(**arguments)
            if name == "git_status":
                return self.git_status(**arguments)
            if name == "git_diff":
                return self.git_diff(**arguments)
            if name == "ssh_execute_command":
                return self.ssh_execute_command(**arguments)
            if name == "ssh_upload_file":
                return self.ssh_upload_file(**arguments)
            if name == "ssh_download_file":
                return self.ssh_download_file(**arguments)
            if name == "ssh_list_directory":
                return self.ssh_list_directory(**arguments)
            if name == "ssh_read_file":
                return self.ssh_read_file(**arguments)
            if name == "ssh_write_file":
                return self.ssh_write_file(**arguments)
            if name == "ssh_make_directory":
                return self.ssh_make_directory(**arguments)
            if name == "ssh_remove_path":
                return self.ssh_remove_path(**arguments)
            if name == "ssh_path_exists":
                return self.ssh_path_exists(**arguments)
            if name == "fetch_web_page":
                return self.fetch_web_page(**arguments)
            if name == "search_web":
                return self.search_web(**arguments)
            if name == "list_knowledge_documents":
                return self.list_knowledge_documents(**arguments)
            if name == "read_knowledge_document":
                return self.read_knowledge_document(**arguments)
            if name == "write_knowledge_document":
                return self.write_knowledge_document(**arguments)
            return ToolExecutionResult(name=name, ok=False, output=f"Unknown tool: {name}")
        except Exception as exc:
            return ToolExecutionResult(name=name, ok=False, output=f"工具执行失败: {exc}")

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
                    "stdout": self._truncate(self._sanitize_shell_output(stdout)),
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

    def find_files(
        self,
        pattern: str = "**/*",
        path: str = ".",
        max_results: int = 200,
        include_hidden: bool = False,
    ) -> ToolExecutionResult:
        """按 glob 模式查找文件，便于代码库导航。"""

        base = Path(self._resolve_path(path))
        if not base.exists():
            return ToolExecutionResult(name="find_files", ok=False, output=f"路径不存在: {base}")
        if base.is_file():
            items = [base]
        else:
            try:
                items = list(base.glob(pattern or "**/*"))
            except ValueError as exc:
                return ToolExecutionResult(name="find_files", ok=False, output=f"glob 模式无效: {exc}")

        results = []
        for item in items:
            if len(results) >= max(1, min(int(max_results), 1000)):
                break
            if not include_hidden and self._is_hidden_path(item, base):
                continue
            if item == base and base.is_dir():
                continue
            results.append(
                {
                    "path": str(item),
                    "relative_path": str(item.relative_to(base)) if item != base and item.is_relative_to(base) else item.name,
                    "type": "dir" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else None,
                }
            )
        return ToolExecutionResult(name="find_files", ok=True, output=json.dumps(results, ensure_ascii=False, indent=2))

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
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with target.open(mode, encoding="utf-8") as handle:
                handle.write(content)
        except PermissionError as exc:
            return ToolExecutionResult(name="write_file", ok=False, output=f"写文件失败，权限不足: {target} ({exc})")
        except OSError as exc:
            return ToolExecutionResult(name="write_file", ok=False, output=f"写文件失败: {target} ({exc})")
        action = "追加" if append else "写入"
        return ToolExecutionResult(name="write_file", ok=True, output=f"已{action}文件: {target}", metadata={"path": str(target)})

    def replace_in_file(
        self,
        path: str,
        search_text: str,
        replace_text: str,
        replace_all: bool = False,
    ) -> ToolExecutionResult:
        """精确替换文件中的文本片段。"""

        allowed, reason = self._check_approval("replace_in_file", path)
        if not allowed:
            return ToolExecutionResult(name="replace_in_file", ok=False, output=reason)

        target = Path(self._resolve_path(path))
        if not target.exists() or not target.is_file():
            return ToolExecutionResult(name="replace_in_file", ok=False, output=f"文件不存在: {target}")
        original = target.read_text(encoding="utf-8", errors="replace")
        count = original.count(search_text)
        if count == 0:
            return ToolExecutionResult(name="replace_in_file", ok=False, output="未找到要替换的文本")
        if count > 1 and not replace_all:
            return ToolExecutionResult(name="replace_in_file", ok=False, output=f"匹配到 {count} 处文本，请开启 replace_all 或提供更精确的 search_text")

        updated = original.replace(search_text, replace_text) if replace_all else original.replace(search_text, replace_text, 1)
        target.write_text(updated, encoding="utf-8")
        diff_preview = "\n".join(
            unified_diff(
                original.splitlines(),
                updated.splitlines(),
                fromfile=str(target),
                tofile=str(target),
                lineterm="",
                n=2,
            )
        )
        return ToolExecutionResult(
            name="replace_in_file",
            ok=True,
            output=self._truncate(diff_preview or f"已更新文件: {target}"),
            metadata={"path": str(target), "occurrences": count},
        )

    def insert_in_file(self, path: str, content: str, line_number: int = 1) -> ToolExecutionResult:
        """按行号插入内容，适合小范围修改。"""

        allowed, reason = self._check_approval("insert_in_file", path)
        if not allowed:
            return ToolExecutionResult(name="insert_in_file", ok=False, output=reason)

        target = Path(self._resolve_path(path))
        if not target.exists() or not target.is_file():
            return ToolExecutionResult(name="insert_in_file", ok=False, output=f"文件不存在: {target}")

        original = target.read_text(encoding="utf-8", errors="replace")
        lines = original.splitlines(keepends=True)
        insertion_index = max(0, min(len(lines), int(line_number) - 1))
        insert_text = content
        if lines and insertion_index < len(lines) and insert_text and not insert_text.endswith(("\n", "\r")):
            insert_text += "\n"
        if not lines and insert_text and not insert_text.endswith(("\n", "\r")):
            insert_text += "\n"
        lines.insert(insertion_index, insert_text)
        updated = "".join(lines)
        target.write_text(updated, encoding="utf-8")
        return ToolExecutionResult(
            name="insert_in_file",
            ok=True,
            output=f"已在第 {insertion_index + 1} 行前插入内容: {target}",
            metadata={"path": str(target), "line_number": insertion_index + 1},
        )

    def list_knowledge_documents(self, keyword: str = "", limit: int = 500) -> ToolExecutionResult:
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

    def write_knowledge_document(self, path: str, content: str, append: bool = False) -> ToolExecutionResult:
        """写入远程知识库文档。"""

        allowed, reason = self._check_approval("write_knowledge_document", path)
        if not allowed:
            return ToolExecutionResult(name="write_knowledge_document", ok=False, output=reason)

        try:
            payload = self.knowledge_base.write_document(path=path, content=content, append=append)
            return ToolExecutionResult(name="write_knowledge_document", ok=True, output=json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:
            return ToolExecutionResult(name="write_knowledge_document", ok=False, output=f"文档写入失败: {exc}")

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

    def get_workspace_overview(self, path: str = ".", max_depth: int = 2, max_entries: int = 80) -> ToolExecutionResult:
        """汇总当前工作区结构、主要文件类型和 Git 状态。"""

        root = Path(self._resolve_path(path))
        if not root.exists():
            return ToolExecutionResult(name="get_workspace_overview", ok=False, output=f"路径不存在: {root}")
        payload = self._collect_workspace_overview(root, max_depth=max_depth, max_entries=max_entries)
        return ToolExecutionResult(name="get_workspace_overview", ok=True, output=json.dumps(payload, ensure_ascii=False, indent=2))

    def git_status(self, repo_path: str = ".", include_untracked: bool = True) -> ToolExecutionResult:
        """读取仓库分支与工作树状态。"""

        repo_root = self._find_git_root(repo_path)
        if repo_root is None:
            return ToolExecutionResult(name="git_status", ok=False, output="当前路径不在 git 仓库中，或系统未安装 git")

        args = ["status", "--short", "--branch"]
        if include_untracked:
            args.append("--untracked-files=all")
        result = self._run_git(repo_root, args)
        if result.returncode != 0:
            return ToolExecutionResult(name="git_status", ok=False, output=result.stderr.strip() or result.stdout.strip() or "git status 执行失败")

        lines = [line for line in result.stdout.splitlines() if line.strip()]
        branch = lines[0] if lines else ""
        entries = []
        for line in lines[1:]:
            status = line[:2]
            file_name = line[3:] if len(line) > 3 else ""
            entries.append({"status": status, "path": file_name})
        payload = {
            "repo_root": str(repo_root),
            "branch": branch,
            "changes": entries,
            "change_count": len(entries),
        }
        return ToolExecutionResult(name="git_status", ok=True, output=json.dumps(payload, ensure_ascii=False, indent=2))

    def git_diff(
        self,
        repo_path: str = ".",
        pathspec: str = "",
        cached: bool = False,
        max_chars: int = 12000,
    ) -> ToolExecutionResult:
        """读取 git diff 文本。"""

        repo_root = self._find_git_root(repo_path)
        if repo_root is None:
            return ToolExecutionResult(name="git_diff", ok=False, output="当前路径不在 git 仓库中，或系统未安装 git")

        args = ["diff", "--no-ext-diff", "--minimal"]
        if cached:
            args.append("--cached")
        if pathspec:
            args.extend(["--", pathspec])
        result = self._run_git(repo_root, args)
        if result.returncode != 0:
            return ToolExecutionResult(name="git_diff", ok=False, output=result.stderr.strip() or result.stdout.strip() or "git diff 执行失败")

        diff_text = result.stdout.strip()
        if not diff_text:
            diff_text = "当前没有差异。"
        return ToolExecutionResult(name="git_diff", ok=True, output=self._truncate(diff_text, limit=max(1000, int(max_chars))))

    def build_workspace_context(self, max_depth: int = 2, max_entries: int = 40) -> str:
        """生成供模型注入的精简工作区上下文。"""

        payload = self._collect_workspace_overview(Path(self.cwd), max_depth=max_depth, max_entries=max_entries)
        lines = ["当前工作区摘要:", f"- 根路径: {payload['root']}"]
        if payload.get("git"):
            git_payload = payload["git"]
            lines.append(f"- Git: {git_payload.get('branch', 'unknown')}")
            if git_payload.get("changes"):
                preview = ", ".join(item["path"] for item in git_payload["changes"][:8])
                suffix = " ..." if len(git_payload["changes"]) > 8 else ""
                lines.append(f"- 已变更文件: {preview}{suffix}")
        if payload.get("languages"):
            lines.append("- 主要文件类型: " + ", ".join(f"{item['extension']}({item['count']})" for item in payload["languages"][:6]))
        if payload.get("sample_files"):
            lines.append("- 代表文件:")
            for item in payload["sample_files"][:12]:
                lines.append(f"  - {item}")
        return "\n".join(lines)

    def ssh_execute_command(
        self,
        host: str,
        username: str,
        command: str,
        port: int = 22,
        password: str = "",
        timeout_seconds: int = 60,
        cwd: Optional[str] = None,
    ) -> ToolExecutionResult:
        """通过 SSH 在远端执行命令。"""

        allowed, policy_reason = self._check_command_policy(command)
        if not allowed:
            return ToolExecutionResult(name="ssh_execute_command", ok=False, output=policy_reason, metadata={"blocked": True})

        display_target = f"{username}@{host}:{port} -> {command}"
        allowed, reason = self._check_approval("ssh_execute_command", display_target)
        if not allowed:
            return ToolExecutionResult(name="ssh_execute_command", ok=False, output=reason)

        try:
            connection = SSHConnectionConfig(
                host=host,
                port=int(port),
                username=username,
                password=password,
                timeout_seconds=min(max(int(timeout_seconds), 1), 600),
            )
            with SSHRemoteClient(connection) as client:
                payload = client.run(command=command, timeout_seconds=timeout_seconds, cwd=cwd)
            payload["stdout"] = self._truncate(str(payload.get("stdout", "")))
            payload["stderr"] = self._truncate(str(payload.get("stderr", "")))
            exit_code = int(payload.get("exit_code", 1))
            return ToolExecutionResult(
                name="ssh_execute_command",
                ok=exit_code == 0,
                output=json.dumps(payload, ensure_ascii=False, indent=2),
                metadata={"exit_code": exit_code, "host": host, "port": port, "username": username},
            )
        except Exception as exc:
            if not password:
                fallback = self._run_ssh_command_via_cli(
                    host=host,
                    port=int(port),
                    username=username,
                    command=command,
                    timeout_seconds=timeout_seconds,
                    cwd=cwd,
                )
                if fallback is not None:
                    exit_code = int(fallback.get("exit_code", 1))
                    return ToolExecutionResult(
                        name="ssh_execute_command",
                        ok=exit_code == 0,
                        output=json.dumps(fallback, ensure_ascii=False, indent=2),
                        metadata={
                            "exit_code": exit_code,
                            "host": host,
                            "port": port,
                            "username": username,
                            "transport": "ssh-cli",
                        },
                    )
            return ToolExecutionResult(name="ssh_execute_command", ok=False, output=f"SSH 命令执行失败: {exc}")

    def ssh_upload_file(
        self,
        host: str,
        username: str,
        local_path: str,
        remote_path: str,
        port: int = 22,
        password: str = "",
    ) -> ToolExecutionResult:
        """通过 SFTP 上传本地文件到远端。"""

        allowed, reason = self._check_approval("ssh_upload_file", f"{local_path} -> {username}@{host}:{remote_path}")
        if not allowed:
            return ToolExecutionResult(name="ssh_upload_file", ok=False, output=reason)

        local = Path(self._resolve_path(local_path))
        if not local.exists() or not local.is_file():
            return ToolExecutionResult(name="ssh_upload_file", ok=False, output=f"本地文件不存在: {local}")

        try:
            with self._open_ssh_client(host, port, username, password) as client:
                client.upload_file(local, remote_path)
            return ToolExecutionResult(
                name="ssh_upload_file",
                ok=True,
                output=json.dumps(
                    {
                        "host": host,
                        "port": port,
                        "username": username,
                        "local_path": str(local),
                        "remote_path": remote_path,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        except Exception as exc:
            if not password and self._upload_file_via_cli(host, int(port), username, local, remote_path):
                return ToolExecutionResult(
                    name="ssh_upload_file",
                    ok=True,
                    output=json.dumps(
                        {
                            "host": host,
                            "port": port,
                            "username": username,
                            "local_path": str(local),
                            "remote_path": remote_path,
                            "transport": "scp-cli",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            return ToolExecutionResult(name="ssh_upload_file", ok=False, output=f"SSH 上传失败: {exc}")

    def ssh_download_file(
        self,
        host: str,
        username: str,
        remote_path: str,
        local_path: str,
        port: int = 22,
        password: str = "",
    ) -> ToolExecutionResult:
        """通过 SFTP 下载远端文件到本地。"""

        local = Path(self._resolve_path(local_path))
        allowed, reason = self._check_approval("ssh_download_file", f"{username}@{host}:{remote_path} -> {local}")
        if not allowed:
            return ToolExecutionResult(name="ssh_download_file", ok=False, output=reason)

        try:
            with self._open_ssh_client(host, port, username, password) as client:
                client.download_file(remote_path, local)
            return ToolExecutionResult(
                name="ssh_download_file",
                ok=True,
                output=json.dumps(
                    {
                        "host": host,
                        "port": port,
                        "username": username,
                        "remote_path": remote_path,
                        "local_path": str(local),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        except Exception as exc:
            if not password and self._download_file_via_cli(host, int(port), username, remote_path, local):
                return ToolExecutionResult(
                    name="ssh_download_file",
                    ok=True,
                    output=json.dumps(
                        {
                            "host": host,
                            "port": port,
                            "username": username,
                            "remote_path": remote_path,
                            "local_path": str(local),
                            "transport": "scp-cli",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            return ToolExecutionResult(name="ssh_download_file", ok=False, output=f"SSH 下载失败: {exc}")

    def ssh_list_directory(
        self,
        host: str,
        username: str,
        path: str,
        port: int = 22,
        password: str = "",
        recursive: bool = False,
        max_entries: int = 200,
    ) -> ToolExecutionResult:
        """列出远端目录内容。"""

        try:
            with self._open_ssh_client(host, port, username, password) as client:
                payload = client.list_directory(path, recursive=recursive, max_entries=max_entries)
            return ToolExecutionResult(name="ssh_list_directory", ok=True, output=json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:
            if not password:
                command = (
                    "python3 - <<'PY'\n"
                    "import json, os\n"
                    f"target = {path!r}\n"
                    f"recursive = {bool(recursive)!r}\n"
                    f"max_entries = {int(max_entries)!r}\n"
                    "results = []\n"
                    "for root, dirs, files in os.walk(target):\n"
                    "    if root != target:\n"
                    "        rel_root = os.path.relpath(root, target)\n"
                    "        results.append({'path': root, 'relative_path': rel_root, 'type': 'dir', 'size': None})\n"
                    "        if len(results) >= max_entries:\n"
                    "            break\n"
                    "    for name in sorted(files):\n"
                    "        current = os.path.join(root, name)\n"
                    "        rel = os.path.relpath(current, target)\n"
                    "        results.append({'path': current, 'relative_path': rel, 'type': 'file', 'size': os.path.getsize(current)})\n"
                    "        if len(results) >= max_entries:\n"
                    "            break\n"
                    "    if len(results) >= max_entries or not recursive:\n"
                    "        if not recursive:\n"
                    "            for name in sorted(dirs):\n"
                    "                current = os.path.join(root, name)\n"
                    "                rel = os.path.relpath(current, target)\n"
                    "                results.append({'path': current, 'relative_path': rel, 'type': 'dir', 'size': None})\n"
                    "                if len(results) >= max_entries:\n"
                    "                    break\n"
                    "        break\n"
                    "print(json.dumps(results, ensure_ascii=False))\n"
                    "PY"
                )
                fallback = self._run_ssh_command_via_cli(host, int(port), username, command, 120, None)
                if fallback is not None and int(fallback.get("exit_code", 1)) == 0:
                    return ToolExecutionResult(name="ssh_list_directory", ok=True, output=self._truncate(str(fallback.get("stdout", ""))))
            return ToolExecutionResult(name="ssh_list_directory", ok=False, output=f"SSH 目录读取失败: {exc}")

    def ssh_read_file(
        self,
        host: str,
        username: str,
        path: str,
        port: int = 22,
        password: str = "",
        start_line: int = 1,
        end_line: int = 200,
    ) -> ToolExecutionResult:
        """读取远端文本文件。"""

        try:
            with self._open_ssh_client(host, port, username, password) as client:
                payload = client.read_file(path, start_line=start_line, end_line=end_line)
            return ToolExecutionResult(name="ssh_read_file", ok=True, output=payload)
        except Exception as exc:
            if not password:
                command = (
                    "awk 'NR>="
                    + str(int(start_line))
                    + " && NR<="
                    + str(int(end_line))
                    + " {printf \"%d: %s\\n\", NR, $0}' "
                    + shlex.quote(path)
                )
                fallback = self._run_ssh_command_via_cli(host, int(port), username, command, 120, None)
                if fallback is not None and int(fallback.get("exit_code", 1)) == 0:
                    return ToolExecutionResult(name="ssh_read_file", ok=True, output=str(fallback.get("stdout", "")).rstrip())
            return ToolExecutionResult(name="ssh_read_file", ok=False, output=f"SSH 文件读取失败: {exc}")

    def ssh_write_file(
        self,
        host: str,
        username: str,
        path: str,
        content: str,
        port: int = 22,
        password: str = "",
        append: bool = False,
    ) -> ToolExecutionResult:
        """写入远端文本文件。"""

        allowed, reason = self._check_approval("ssh_write_file", f"{username}@{host}:{path}")
        if not allowed:
            return ToolExecutionResult(name="ssh_write_file", ok=False, output=reason)

        try:
            with self._open_ssh_client(host, port, username, password) as client:
                client.write_file(path, content, append=append)
            return ToolExecutionResult(
                name="ssh_write_file",
                ok=True,
                output=json.dumps(
                    {
                        "host": host,
                        "port": port,
                        "username": username,
                        "path": path,
                        "append": append,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        except Exception as exc:
            if not password:
                import tempfile

                with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
                    handle.write(content)
                    temp_path = Path(handle.name)
                try:
                    if append:
                        upload_ok = self._upload_file_via_cli(host, int(port), username, temp_path, f"{path}.lumin-chat.tmp")
                        if upload_ok:
                            parent_path = str(Path(path).parent).replace("\\", "/")
                            command = (
                                f"mkdir -p {shlex.quote(parent_path)} && "
                                f"cat {shlex.quote(path + '.lumin-chat.tmp')} >> {shlex.quote(path)} && "
                                f"rm -f {shlex.quote(path + '.lumin-chat.tmp')}"
                            )
                            fallback = self._run_ssh_command_via_cli(host, int(port), username, command, 120, None)
                            if fallback is not None and int(fallback.get("exit_code", 1)) == 0:
                                return ToolExecutionResult(name="ssh_write_file", ok=True, output=json.dumps({"path": path, "append": append, "transport": "ssh-cli"}, ensure_ascii=False, indent=2))
                    elif self._upload_file_via_cli(host, int(port), username, temp_path, path):
                        return ToolExecutionResult(name="ssh_write_file", ok=True, output=json.dumps({"path": path, "append": append, "transport": "scp-cli"}, ensure_ascii=False, indent=2))
                finally:
                    temp_path.unlink(missing_ok=True)
            return ToolExecutionResult(name="ssh_write_file", ok=False, output=f"SSH 文件写入失败: {exc}")

    def ssh_make_directory(
        self,
        host: str,
        username: str,
        path: str,
        port: int = 22,
        password: str = "",
    ) -> ToolExecutionResult:
        """创建远端目录。"""

        allowed, reason = self._check_approval("ssh_make_directory", f"{username}@{host}:{path}")
        if not allowed:
            return ToolExecutionResult(name="ssh_make_directory", ok=False, output=reason)

        try:
            with self._open_ssh_client(host, port, username, password) as client:
                client.ensure_remote_dir(path)
            return ToolExecutionResult(name="ssh_make_directory", ok=True, output=f"已创建远端目录: {path}")
        except Exception as exc:
            if not password:
                fallback = self._run_ssh_command_via_cli(host, int(port), username, f"mkdir -p {shlex.quote(path)}", 120, None)
                if fallback is not None and int(fallback.get("exit_code", 1)) == 0:
                    return ToolExecutionResult(name="ssh_make_directory", ok=True, output=f"已创建远端目录: {path}")
            return ToolExecutionResult(name="ssh_make_directory", ok=False, output=f"SSH 目录创建失败: {exc}")

    def ssh_remove_path(
        self,
        host: str,
        username: str,
        path: str,
        port: int = 22,
        password: str = "",
    ) -> ToolExecutionResult:
        """删除远端路径。"""

        allowed, reason = self._check_approval("ssh_remove_path", f"{username}@{host}:{path}")
        if not allowed:
            return ToolExecutionResult(name="ssh_remove_path", ok=False, output=reason)

        try:
            with self._open_ssh_client(host, port, username, password) as client:
                payload = client.remove_remote_path(path)
            exit_code = int(payload.get("exit_code", 1))
            payload["stdout"] = self._truncate(str(payload.get("stdout", "")))
            payload["stderr"] = self._truncate(str(payload.get("stderr", "")))
            return ToolExecutionResult(
                name="ssh_remove_path",
                ok=exit_code == 0,
                output=json.dumps(payload, ensure_ascii=False, indent=2),
                metadata={"exit_code": exit_code},
            )
        except Exception as exc:
            if not password:
                fallback = self._run_ssh_command_via_cli(host, int(port), username, f"rm -rf {shlex.quote(path)}", 120, None)
                if fallback is not None:
                    exit_code = int(fallback.get("exit_code", 1))
                    return ToolExecutionResult(
                        name="ssh_remove_path",
                        ok=exit_code == 0,
                        output=json.dumps(fallback, ensure_ascii=False, indent=2),
                        metadata={"exit_code": exit_code},
                    )
            return ToolExecutionResult(name="ssh_remove_path", ok=False, output=f"SSH 路径删除失败: {exc}")

    def ssh_path_exists(
        self,
        host: str,
        username: str,
        path: str,
        port: int = 22,
        password: str = "",
    ) -> ToolExecutionResult:
        """检查远端路径是否存在。"""

        try:
            with self._open_ssh_client(host, port, username, password) as client:
                exists = client.path_exists(path)
            return ToolExecutionResult(
                name="ssh_path_exists",
                ok=True,
                output=json.dumps(
                    {
                        "host": host,
                        "port": port,
                        "username": username,
                        "path": path,
                        "exists": exists,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        except Exception as exc:
            if not password:
                fallback = self._run_ssh_command_via_cli(host, int(port), username, f"test -e {shlex.quote(path)}", 120, None)
                if fallback is not None:
                    return ToolExecutionResult(
                        name="ssh_path_exists",
                        ok=True,
                        output=json.dumps(
                            {
                                "host": host,
                                "port": port,
                                "username": username,
                                "path": path,
                                "exists": int(fallback.get("exit_code", 1)) == 0,
                                "transport": "ssh-cli",
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
            return ToolExecutionResult(name="ssh_path_exists", ok=False, output=f"SSH 路径检查失败: {exc}")

    def fetch_web_page(self, url: str, timeout_seconds: int = 60, max_chars: int = 12000) -> ToolExecutionResult:
        """抓取网页并提取可读正文。"""

        try:
            self.web_client.timeout_seconds = min(max(int(timeout_seconds), 1), 600)
            payload = self.web_client.fetch_page(url=url, max_chars=max_chars)
            return ToolExecutionResult(name="fetch_web_page", ok=True, output=format_payload(payload))
        except Exception as exc:
            return ToolExecutionResult(name="fetch_web_page", ok=False, output=f"网页抓取失败: {exc}")

    def search_web(self, query: str, limit: int = 5, timeout_seconds: int = 60) -> ToolExecutionResult:
        """执行公开网页搜索。"""

        try:
            self.web_client.timeout_seconds = min(max(int(timeout_seconds), 1), 600)
            payload = self.web_client.search(query=query, limit=max(1, min(int(limit), 10)))
            return ToolExecutionResult(name="search_web", ok=True, output=format_payload(payload))
        except Exception as exc:
            return ToolExecutionResult(name="search_web", ok=False, output=f"网页搜索失败: {exc}")

    @staticmethod
    def _open_ssh_client(host: str, port: int, username: str, password: str) -> SSHRemoteClient:
        """构造 SSH 客户端。"""

        connection = SSHConnectionConfig(
            host=host,
            port=int(port),
            username=username,
            password=password,
            timeout_seconds=60,
        )
        return SSHRemoteClient(connection)

    def _resolve_path(self, raw_path: str) -> str:
        """将相对路径解析为基于当前 cwd 的绝对路径。"""

        expanded = os.path.expanduser(raw_path)
        candidate = Path(expanded)
        if candidate.is_absolute():
            return str(candidate.resolve())
        return str((Path(self.cwd) / candidate).resolve())

    def _collect_workspace_overview(self, root: Path, max_depth: int, max_entries: int) -> Dict[str, object]:
        """收集工作区摘要，供工具和系统提示词复用。"""

        root = root.resolve()
        sample_files: List[str] = []
        directories: List[str] = []
        extensions: Dict[str, int] = {}
        limit = max(10, min(int(max_entries), 500))
        for path in root.rglob("*"):
            if len(sample_files) >= limit and len(directories) >= limit:
                break
            if self._is_hidden_path(path, root):
                continue
            try:
                relative = str(path.relative_to(root))
            except ValueError:
                relative = path.name
            depth = len(Path(relative).parts)
            if depth > max(1, int(max_depth)) + 1:
                continue
            if path.is_dir():
                if len(directories) < limit:
                    directories.append(relative)
                continue
            if len(sample_files) < limit:
                sample_files.append(relative)
            extension = path.suffix.lower() or "<no_ext>"
            extensions[extension] = extensions.get(extension, 0) + 1

        languages = [
            {"extension": extension, "count": count}
            for extension, count in sorted(extensions.items(), key=lambda item: (-item[1], item[0]))
        ]
        payload: Dict[str, object] = {
            "root": str(root),
            "directories": directories[:limit],
            "sample_files": sample_files[:limit],
            "languages": languages[:10],
        }
        repo_root = self._find_git_root(str(root))
        if repo_root is not None:
            status_result = self.git_status(str(repo_root))
            if status_result.ok:
                try:
                    payload["git"] = json.loads(status_result.output)
                except json.JSONDecodeError:
                    payload["git"] = {"raw": status_result.output}
        return payload

    @staticmethod
    def _is_hidden_path(path: Path, root: Path) -> bool:
        """判断路径是否位于隐藏目录或本身为隐藏文件。"""

        try:
            parts = path.relative_to(root).parts
        except ValueError:
            parts = path.parts
        return any(part.startswith(".") for part in parts if part not in {".", ".."})

    def _find_git_root(self, repo_path: str) -> Optional[Path]:
        """查找给定路径所在的 git 仓库根目录。"""

        if shutil.which("git") is None:
            return None
        target = Path(self._resolve_path(repo_path))
        working_dir = target if target.is_dir() else target.parent
        result = self._run_git(working_dir, ["rev-parse", "--show-toplevel"])
        if result.returncode != 0:
            return None
        root = result.stdout.strip()
        return Path(root).resolve() if root else None

    @staticmethod
    def _run_git(repo_root: Path, args: List[str]) -> subprocess.CompletedProcess[str]:
        """在指定仓库执行 git 命令。"""

        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )

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

        valid_keys = self.TOOL_ALLOWED_KEYS.get(tool_name)
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

        recovered = self._recover_loose_object(tool_name, text)
        if recovered:
            return recovered

        primary_field = self.TOOL_PRIMARY_FIELDS.get(tool_name)
        if primary_field is None:
            return {}
        return {primary_field: text}

    def _recover_loose_object(self, tool_name: str, text: str) -> Dict[str, object]:
        """从不完整或转义错误的 JSON 风格文本中尽量恢复字段。"""

        valid_keys = sorted(self.TOOL_ALLOWED_KEYS.get(tool_name, set()), key=len, reverse=True)
        if not valid_keys:
            return {}

        pattern = re.compile(r'"(?P<key>' + "|".join(re.escape(item) for item in valid_keys) + r')"\s*:\s*', re.DOTALL)
        matches = list(pattern.finditer(text))
        if not matches:
            return {}

        recovered: Dict[str, object] = {}
        for index, match in enumerate(matches):
            key = match.group("key")
            value_start = match.end()
            value_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            raw_segment = text[value_start:value_end].lstrip()
            parsed = self._parse_loose_value(raw_segment)
            if parsed is not None:
                recovered[key] = parsed
        return recovered

    def _parse_loose_value(self, raw_segment: str) -> object:
        """解析字段值，兼容缺失引号与中途截断。"""

        if not raw_segment:
            return None

        if raw_segment.startswith('"'):
            return self._parse_loose_string(raw_segment[1:])

        lowered = raw_segment.lower()
        if lowered.startswith("true"):
            return True
        if lowered.startswith("false"):
            return False
        if lowered.startswith("null"):
            return None

        number_match = re.match(r"-?\d+", raw_segment)
        if number_match:
            return int(number_match.group(0))

        token = raw_segment.strip().rstrip(",").rstrip("}").strip()
        return token or None

    def _parse_loose_string(self, text: str) -> str:
        """解析宽松的双引号字符串，容忍内部裸引号与缺失收尾引号。"""

        buffer: List[str] = []
        index = 0
        while index < len(text):
            char = text[index]
            if char == "\\":
                if index + 1 < len(text):
                    buffer.append(char)
                    buffer.append(text[index + 1])
                    index += 2
                    continue
                buffer.append(char)
                break
            if char == '"':
                tail = text[index + 1 :]
                if re.match(r"\s*(,|}|$)", tail):
                    break
                buffer.append(char)
                index += 1
                continue
            buffer.append(char)
            index += 1

        raw_value = "".join(buffer)
        try:
            return codecs_decode(raw_value, "unicode_escape")
        except Exception:
            return raw_value

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

    @staticmethod
    def _sanitize_shell_output(text: str) -> str:
        """清理 PTY 回显中的控制字符和空提示符残留。"""

        cleaned = text.replace("\r", "")
        cleaned = re.sub(r"\x1b\[\?2004[hl]", "", cleaned)
        cleaned = re.sub(r"\n>\s*$", "", cleaned)
        cleaned = re.sub(r"^>\s*$", "", cleaned, flags=re.MULTILINE)
        return cleaned.strip()

    def _run_ssh_command_via_cli(
        self,
        host: str,
        port: int,
        username: str,
        command: str,
        timeout_seconds: int,
        cwd: Optional[str],
    ) -> Optional[Dict[str, object]]:
        """在密钥登录场景下回退到系统 ssh 命令。"""

        remote_command = command if not cwd else f"cd {shlex.quote(cwd)} && {command}"
        target = f"{username}@{host}" if username else host
        try:
            completed = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-p",
                    str(port),
                    target,
                    remote_command,
                ],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except Exception:
            return None

        return {
            "command": command,
            "cwd": cwd or "",
            "exit_code": completed.returncode,
            "stdout": self._truncate(completed.stdout),
            "stderr": self._truncate(completed.stderr),
            "host": host,
            "port": port,
            "username": username,
            "transport": "ssh-cli",
        }

    @staticmethod
    def _upload_file_via_cli(host: str, port: int, username: str, local_path: Path, remote_path: str) -> bool:
        """使用 scp 上传文件，作为无 Paramiko 场景下的回退方案。"""

        target = f"{username}@{host}" if username else host
        try:
            parent = Path(remote_path).parent.as_posix()
            subprocess.run(
                [
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-p",
                    str(port),
                    target,
                    f"mkdir -p {shlex.quote(parent)}",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )
            subprocess.run(
                [
                    "scp",
                    "-O",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-P",
                    str(port),
                    str(local_path),
                    f"{target}:{remote_path}",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )
            return True
        except Exception:
            return False

    @staticmethod
    def _download_file_via_cli(host: str, port: int, username: str, remote_path: str, local_path: Path) -> bool:
        """使用 scp 下载文件，作为无 Paramiko 场景下的回退方案。"""

        target = f"{username}@{host}" if username else host
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "scp",
                    "-O",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-P",
                    str(port),
                    f"{target}:{remote_path}",
                    str(local_path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )
            return True
        except Exception:
            return False
