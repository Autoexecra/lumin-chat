import argparse
import json
import os
from typing import Optional

from src.agent import CopilotTerminalAgent
from src.config_loader import load_config
from src.ui import TerminalUI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Copilot-like terminal agent")
    parser.add_argument("--config", default="config.json", help="Path to config file")
    parser.add_argument("--model-level", type=int, help="Model level to use")
    parser.add_argument(
        "--approval-mode",
        choices=["prompt", "auto", "read-only"],
        help="Tool approval mode",
    )
    parser.add_argument("--workdir", default=os.getcwd(), help="Initial working directory")
    parser.add_argument("--session", help="Resume an existing session by id or path")
    parser.add_argument("--show-thinking", action="store_true", help="Force enable thinking stream")
    parser.add_argument("--hide-thinking", action="store_true", help="Hide thinking stream")

    subparsers = parser.add_subparsers(dest="command")

    chat_parser = subparsers.add_parser("chat", help="Start interactive chat")
    chat_parser.set_defaults(command="chat")

    ask_parser = subparsers.add_parser("ask", help="Run a single non-interactive request")
    ask_parser.add_argument("prompt", help="Prompt to send to the agent")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        args.command = "chat"

    config = load_config(args.config)
    app_config = config.get("app", {})
    model_level = args.model_level or int(app_config.get("default_model_level", 3))
    approval_mode = args.approval_mode or app_config.get("default_approval_policy", "prompt")
    show_thinking = bool(app_config.get("show_thinking", True))
    if args.show_thinking:
        show_thinking = True
    if args.hide_thinking:
        show_thinking = False

    ui = TerminalUI(show_thinking=show_thinking)
    agent = CopilotTerminalAgent(
        config=config,
        ui=ui,
        model_level=model_level,
        approval_policy=approval_mode,
        workdir=args.workdir,
        session_id_or_path=args.session,
    )

    if args.command == "ask":
        agent.run(args.prompt)
        return 0

    ui.show_banner(
        model_name=agent.describe_model(),
        cwd=agent.cwd,
        approval_policy=agent.session.approval_policy,
        session_path=str(agent.session_path),
    )

    while True:
        try:
            user_input = input("you> ").strip()
        except EOFError:
            return 0
        except KeyboardInterrupt:
            print()
            return 0

        if not user_input:
            continue
        if user_input.startswith("/"):
            if not _handle_slash_command(user_input, agent, ui):
                return 0
            continue
        agent.run(user_input)


def _handle_slash_command(raw: str, agent: CopilotTerminalAgent, ui: TerminalUI) -> bool:
    parts = raw.split(maxsplit=1)
    command = parts[0].lower()
    argument = parts[1] if len(parts) > 1 else ""

    if command in {"/exit", "/quit"}:
        return False
    if command == "/help":
        ui.show_info(
            "/help /exit /reset /model <n> /approval <prompt|auto|read-only> /cd <path> /cwd /session /shell /restart-shell"
        )
        return True
    if command == "/reset":
        agent.reset_session()
        ui.show_info(f"新会话已创建: {agent.session_path}")
        return True
    if command == "/model":
        if not argument:
            ui.show_warning("用法: /model <level>")
            return True
        agent.set_model_level(int(argument))
        ui.show_info(f"模型已切换到 {agent.describe_model()}")
        return True
    if command == "/approval":
        if argument not in {"prompt", "auto", "read-only"}:
            ui.show_warning("用法: /approval <prompt|auto|read-only>")
            return True
        agent.set_approval_policy(argument)
        ui.show_info(f"审批策略已切换到 {argument}")
        return True
    if command == "/cd":
        if not argument:
            ui.show_warning("用法: /cd <path>")
            return True
        ui.show_info(agent.change_directory(argument))
        return True
    if command == "/cwd":
        ui.show_info(agent.cwd)
        return True
    if command == "/session":
        ui.show_info(str(agent.session_path))
        return True
    if command == "/shell":
        ui.show_info(json.dumps(agent.shell_state(), ensure_ascii=False, indent=2))
        return True
    if command == "/restart-shell":
        agent.restart_shell()
        ui.show_info("shell 会话已重启")
        return True

    ui.show_warning(f"未知命令: {raw}")
    return True


if __name__ == "__main__":
    raise SystemExit(main())
