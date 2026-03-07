import json
from typing import Any, Dict

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

from src.models import ToolExecutionResult


class TerminalUI:
    def __init__(self, show_thinking: bool = True):
        self.console = Console()
        self.show_thinking = show_thinking
        self._stream_mode = None

    def show_banner(self, model_name: str, cwd: str, approval_policy: str, session_path: str) -> None:
        text = (
            f"model={model_name}\n"
            f"cwd={cwd}\n"
            f"approval={approval_policy}\n"
            f"session={session_path}\n"
            "slash commands: /help /model /approval /cd /cwd /session /reset /exit"
        )
        self.console.print(Panel(text, title="Copilot Terminal Clone", border_style="cyan"))

    def stream_reasoning(self, text: str) -> None:
        if not self.show_thinking:
            return
        if self._stream_mode != "reasoning":
            self.console.print("\nthinking> ", style="dim", end="")
            self._stream_mode = "reasoning"
        self.console.print(text, style="dim", end="", markup=False, highlight=False)

    def stream_content(self, text: str) -> None:
        if self._stream_mode != "content":
            self.console.print("\nassistant> ", style="bold cyan", end="")
            self._stream_mode = "content"
        self.console.print(text, end="", markup=False, highlight=False)

    def end_stream(self) -> None:
        if self._stream_mode is not None:
            self.console.print()
        self._stream_mode = None

    def show_tool_call(self, name: str, arguments: Dict[str, Any]) -> None:
        payload = json.dumps(arguments, ensure_ascii=False, indent=2)
        self.console.print(Panel(payload, title=f"tool> {name}", border_style="yellow"))

    def show_tool_result(self, result: ToolExecutionResult) -> None:
        style = "green" if result.ok else "red"
        self.console.print(Panel(result.output, title=f"result> {result.name}", border_style=style))

    def show_info(self, message: str) -> None:
        self.console.print(message, style="cyan")

    def show_warning(self, message: str) -> None:
        self.console.print(message, style="yellow")

    def show_error(self, message: str) -> None:
        self.console.print(message, style="bold red")

    def confirm(self, title: str, details: str) -> bool:
        prompt = f"{title}: {details}\n允许执行?"
        return Confirm.ask(prompt, default=False)
