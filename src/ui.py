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
        self._suppress_think_tag = False
        self._content_visible_started = False

    def show_banner(self, model_name: str, cwd: str, approval_policy: str, command_policy_mode: str, session_path: str) -> None:
        """显示启动横幅。"""

        text = (
            f"model={model_name}\n"
            f"cwd={cwd}\n"
            f"approval={approval_policy}\n"
            f"command_policy={command_policy_mode}\n"
            f"session={session_path}\n"
            "slash commands: /help /new-session /sessions /switch-session /model /approval /policy /cd /cwd /session /shell /memory /restart-shell /reset /exit"
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

        if not self.show_thinking:
            text = self._strip_hidden_thinking(text)
            if not text:
                return

        if self._stream_mode != "content":
            self.console.print("\nassistant> ", style="bold cyan", end="")
            self._stream_mode = "content"
        self.console.print(text, end="", markup=False, highlight=False)
        self._content_visible_started = True

    def end_stream(self) -> None:
        """结束一次流式输出。"""

        if self._stream_mode is not None:
            self.console.print()
        self._stream_mode = None
        self._suppress_think_tag = False
        self._content_visible_started = False

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

    def _strip_hidden_thinking(self, text: str) -> str:
        """在隐藏 thinking 时过滤 <think>...</think> 内容。"""

        remaining = text
        output_parts = []
        while remaining:
            if self._suppress_think_tag:
                end_index = remaining.find("</think>")
                if end_index == -1:
                    return ""
                remaining = remaining[end_index + len("</think>") :]
                self._suppress_think_tag = False
                continue

            if not self._content_visible_started and "</think>" in remaining and "<think>" not in remaining:
                remaining = remaining.split("</think>", 1)[1]
                continue

            start_index = remaining.find("<think>")
            if start_index == -1:
                output_parts.append(remaining)
                break
            if start_index > 0:
                output_parts.append(remaining[:start_index])
            remaining = remaining[start_index + len("<think>") :]
            end_index = remaining.find("</think>")
            if end_index == -1:
                self._suppress_think_tag = True
                break
            remaining = remaining[end_index + len("</think>") :]
        return "".join(output_parts)
