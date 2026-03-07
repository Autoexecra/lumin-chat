"""终端输出渲染。"""

import json
from typing import Any, Dict

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

from src.models import ToolExecutionResult


class TerminalUI:
    """负责把代理状态渲染到终端。"""

    def __init__(self, show_thinking: bool = True):
        """初始化 Rich 终端输出。"""

        self.console = Console()
        self.show_thinking = show_thinking
        self._stream_mode = None

    def show_banner(self, model_name: str, cwd: str, approval_policy: str, command_policy_mode: str, session_path: str) -> None:
        """显示启动横幅。"""

        text = (
            f"model={model_name}\n"
            f"cwd={cwd}\n"
            f"approval={approval_policy}\n"
            f"command_policy={command_policy_mode}\n"
            f"session={session_path}\n"
            "slash commands: /help /model /approval /policy /cd /cwd /session /shell /restart-shell /reset /exit"
        )
        self.console.print(Panel(text, title="lumin-chat", border_style="cyan"))

    def stream_reasoning(self, text: str) -> None:
        """输出推理流。"""

        if not self.show_thinking:
            return
        if self._stream_mode != "reasoning":
            self.console.print("\nthinking> ", style="dim", end="")
            self._stream_mode = "reasoning"
        self.console.print(text, style="dim", end="", markup=False, highlight=False)

    def stream_content(self, text: str) -> None:
        """输出回答正文流。"""

        if self._stream_mode != "content":
            self.console.print("\nassistant> ", style="bold cyan", end="")
            self._stream_mode = "content"
        self.console.print(text, end="", markup=False, highlight=False)

    def end_stream(self) -> None:
        """结束一次流式输出。"""

        if self._stream_mode is not None:
            self.console.print()
        self._stream_mode = None

    def show_tool_call(self, name: str, arguments: Dict[str, Any]) -> None:
        """展示工具调用参数。"""

        payload = json.dumps(arguments, ensure_ascii=False, indent=2)
        self.console.print(Panel(payload, title=f"tool> {name}", border_style="yellow"))

    def show_tool_result(self, result: ToolExecutionResult) -> None:
        """展示工具执行结果。"""

        style = "green" if result.ok else "red"
        self.console.print(Panel(result.output, title=f"result> {result.name}", border_style=style))

    def show_info(self, message: str) -> None:
        """输出普通提示。"""

        self.console.print(message, style="cyan")

    def show_warning(self, message: str) -> None:
        """输出警告提示。"""

        self.console.print(message, style="yellow")

    def show_error(self, message: str) -> None:
        """输出错误提示。"""

        self.console.print(message, style="bold red")

    def confirm(self, title: str, details: str) -> bool:
        """在需要时向用户请求确认。"""

        prompt = f"{title}: {details}\n允许执行?"
        return Confirm.ask(prompt, default=False)
