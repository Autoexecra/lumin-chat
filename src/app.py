# Copyright (c) 2026 Autoexecra
# Licensed under the Apache License, Version 2.0.
# See LICENSE in the project root for license terms.

"""lumin-chat 命令行解析与交互循环。"""

import argparse
import json
import os
import sys
from typing import Optional

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
except ImportError:
    PromptSession = None
    FileHistory = None

from src.agent import LuminChatAgent
from src.batch_runner import BatchTaskRunner
from src.config_loader import load_config
from src.license_guard import validate_runtime_license
from src.ui import TerminalUI


def ensure_utf8_stdio() -> None:
    """尽量把标准输入输出切换到 UTF-8，降低中文乱码概率。"""

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def build_prompt_session(workdir: str) -> Optional["PromptSession[str]"]:
    """构造具备历史记录的交互输入会话。"""

    if PromptSession is None or FileHistory is None:
        return None
    history_dir = os.path.join(workdir, ".lumin-chat")
    os.makedirs(history_dir, exist_ok=True)
    history_path = os.path.join(history_dir, "history")
    return PromptSession(history=FileHistory(history_path))


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""

    parser = argparse.ArgumentParser(description="lumin-chat 终端代理")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--model-level", type=int, help="要使用的模型级别")
    parser.add_argument(
        "--approval-mode",
        choices=["prompt", "auto", "read-only"],
        help="工具审批模式",
    )
    parser.add_argument(
        "--command-policy-mode",
        choices=["blacklist", "whitelist"],
        help="Shell 命令策略模式",
    )
    parser.add_argument("--workdir", default=os.getcwd(), help="初始工作目录")
    parser.add_argument("--session", help="按会话 ID 或路径恢复历史会话")
    parser.add_argument("--show-thinking", action="store_true", help="强制显示 thinking 流")
    parser.add_argument("--hide-thinking", action="store_true", help="隐藏 thinking 流")

    subparsers = parser.add_subparsers(dest="command")

    chat_parser = subparsers.add_parser("chat", help="启动交互式对话")
    chat_parser.set_defaults(command="chat")

    ask_parser = subparsers.add_parser("ask", help="执行一次非交互请求")
    ask_parser.add_argument("prompt", help="发送给代理的提示词")

    batch_parser = subparsers.add_parser("batch", help="按 JSON 文件批量执行任务")
    batch_parser.add_argument("task_file", help="批量任务 JSON 文件路径")
    batch_parser.add_argument("--report-dir", help="报告输出目录，默认 ~/lumin-report")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """运行命令行主入口。"""

    ensure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        args.command = "chat"

    config = load_config(args.config)
    license_result = validate_runtime_license(config)
    if not license_result.ok:
        TerminalUI(show_thinking=False).show_error(license_result.message)
        return 2

    app_config = config.get("app", {})
    model_level = args.model_level or int(app_config.get("default_model_level", 3))
    approval_mode = args.approval_mode or app_config.get("default_approval_policy", "prompt")
    command_policy_mode = args.command_policy_mode or config.get("command_policy", {}).get("mode", "blacklist")
    show_thinking = bool(app_config.get("show_thinking", True))
    if args.show_thinking:
        show_thinking = True
    if args.hide_thinking:
        show_thinking = False

    ui = TerminalUI(show_thinking=show_thinking)
    def create_agent() -> LuminChatAgent:
        agent = LuminChatAgent(
            config=config,
            ui=ui,
            model_level=model_level,
            approval_policy=approval_mode,
            workdir=args.workdir,
            session_id_or_path=args.session,
        )
        agent.set_command_policy_mode(command_policy_mode)
        return agent

    if args.command == "batch":
        runner = BatchTaskRunner(
            agent_factory=create_agent,
            report_dir=getattr(args, "report_dir", None) or app_config.get("report_dir", "~/lumin-report"),
        )
        results = runner.run_file(args.task_file, report_dir=getattr(args, "report_dir", None))
        ui.show_info(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    agent = create_agent()

    if args.command == "ask":
        agent.run(args.prompt)
        return 0

    ui.show_banner(
        model_name=agent.describe_model(),
        cwd=agent.cwd,
        approval_policy=agent.session.approval_policy,
        command_policy_mode=agent.command_policy_state().get("mode", "blacklist"),
        session_path=str(agent.session_path),
    )
    prompt_session = build_prompt_session(agent.cwd)

    while True:
        try:
            if prompt_session is not None:
                user_input = prompt_session.prompt("you> ").strip()
            else:
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


def _handle_slash_command(raw: str, agent: LuminChatAgent, ui: TerminalUI) -> bool:
    """处理交互模式下的斜杠命令。"""

    parts = raw.split(maxsplit=1)
    command = parts[0].lower()
    argument = parts[1] if len(parts) > 1 else ""

    if command in {"/exit", "/quit"}:
        return False
    if command == "/help":
        ui.show_info(
            """
/help               - 显示帮助信息
/exit, /quit       - 退出交互模式
/reset              - 重置当前会话（新会话）
/new-session        - 创建并切换到新会话
/sessions [n]       - 列出最近 n 个会话（默认 20）
/switch-session id  - 切换到指定会话（ID 或路径）
/model <n>          - 切换模型等级（1-5）
/approval <mode>    - 切换审批策略（prompt/auto/read-only）
/policy <mode>      - 切换命令策略（blacklist/whitelist）
/cd <path>          - 切换工作目录
/cwd                - 显示当前工作目录
/session            - 显示当前会话路径
/shell              - 显示 shell 状态
/memory [query]     - 显示当前会话记忆摘要（可选 query）
/restart-shell      - 重启 Shell 会话
"""
        )
        return True
    if command == "/reset":
        agent.reset_session()
        ui.show_info(f"新会话已创建: {agent.session_path}")
        return True
    if command == "/new-session":
        session_id = agent.create_new_session()
        ui.show_info(f"已切换到新会话: {session_id}")
        return True
    if command == "/sessions":
        limit = 20
        if argument.strip().isdigit():
            limit = max(1, min(int(argument.strip()), 100))
        ui.show_info(json.dumps(agent.list_sessions(limit=limit), ensure_ascii=False, indent=2))
        return True
    if command == "/switch-session":
        if not argument:
            ui.show_warning("用法: /switch-session <session_id|path>")
            return True
        session_id = agent.switch_session(argument)
        ui.show_info(f"已切换到会话: {session_id}")
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
    if command == "/policy":
        if argument not in {"blacklist", "whitelist"}:
            ui.show_warning("用法: /policy <blacklist|whitelist>")
            return True
        agent.set_command_policy_mode(argument)
        ui.show_info(f"命令策略已切换到 {argument}")
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
    if command == "/memory":
        ui.show_info(agent.memory_summary(argument))
        return True
    if command == "/restart-shell":
        agent.restart_shell()
        ui.show_info("shell 会话已重启")
        return True

    ui.show_warning(f"未知命令: {raw}")
    return True


if __name__ == "__main__":
    raise SystemExit(main())
