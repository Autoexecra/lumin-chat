# Copyright (c) 2026 Autoexecra
# Licensed under the Apache License, Version 2.0.
# See LICENSE in the project root for license terms.

"""通过 SSH 在远端开发板上执行 Docker Ubuntu 非交互测试并生成中文报告。"""

import argparse
import datetime as dt
import json
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = PROJECT_ROOT / "reports" / "docker_ubuntu_test_report.md"


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """执行本地外部命令并捕获文本输出。"""

    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")


def remote_run(host: str, port: int, user: str, remote_command: str) -> subprocess.CompletedProcess[str]:
    """通过 SSH 执行远端命令。"""

    return run(["ssh", "-p", str(port), f"{user}@{host}", remote_command])


def sanitize_output(text: str) -> str:
    """移除 SSH 登录横幅和已知的 Windows OpenSSH 噪声。"""

    if not text:
        return ""

    lines = text.splitlines()
    filtered: list[str] = []
    banner_markers = (
        "QSemOS(",
        "____",
        "/ __ \\",
        "/ / / /",
        "/ /_/ /",
        "\\___\\_\\",
        "Authorized uses only. All activity may be monitored and reported.",
        "close - IO is still pending on closed socket.",
    )
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if filtered and filtered[-1] != "":
                filtered.append("")
            continue
        if any(marker in stripped for marker in banner_markers):
            continue
        if stripped.startswith("PS "):
            continue
        filtered.append(line)

    return "\n".join(filtered).strip()


def build_test_cases(
    remote_dir: str = "/var/lib/lumin-chat",
    launcher_command: str = "/usr/bin/lumin-chat --help",
    config_path: str = "/etc/lumin-chat/config.json",
) -> list[dict[str, str]]:
    """定义本次 Docker Ubuntu 回归测试的命令集合。"""

    return [
        {
            "name": "检查 RPM 安装状态",
            "command": "rpm -q lumin-chat",
        },
        {
            "name": "检查 Docker 版本",
            "command": "docker --version",
        },
        {
            "name": "检查系统配置文件",
            "command": f"test -f {config_path} && python3 - <<'PY'\nimport json\nwith open('{config_path}', 'r', encoding='utf-8') as handle:\n    payload = json.load(handle)\nprint(payload.get('app', {{}}).get('default_model_level', 'missing'))\nPY",
        },
        {
            "name": "检查启动脚本帮助信息",
            "command": launcher_command,
        },
        {
            "name": "执行项目冒烟测试",
            "command": (
                f"cd {remote_dir} && "
                "if [ -x .venv/bin/python ]; then .venv/bin/python scripts/smoke_test.py; "
                "elif [ -d vendor ]; then PYTHONPATH=vendor python3 scripts/smoke_test.py; "
                "else python3 scripts/smoke_test.py; fi"
            ),
        },
        {
            "name": "拉取 Ubuntu 镜像",
            "command": "update-ca-trust extract >/dev/null 2>&1 || true; systemctl restart docker >/dev/null 2>&1 || true; docker pull ubuntu:latest",
        },
        {
            "name": "读取 Ubuntu 系统信息",
            "command": "docker run --rm ubuntu:latest sh -lc 'cat /etc/os-release | sed -n \"1,8p\"'",
        },
        {
            "name": "验证容器内文件写入",
            "command": "docker run --rm ubuntu:latest sh -lc 'mkdir -p /tmp/lumin-chat && echo ready > /tmp/lumin-chat/status.txt && cat /tmp/lumin-chat/status.txt'",
        },
        {
            "name": "验证容器内目录遍历",
            "command": "docker run --rm ubuntu:latest sh -lc 'pwd && ls / | sed -n \"1,20p\"'",
        },
    ]


def render_report(host: str, port: int, user: str, remote_dir: str, results: list[dict[str, object]]) -> str:
    """将测试结果整理成中文 Markdown 报告。"""

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    passed = sum(1 for item in results if item["returncode"] == 0)
    total = len(results)
    lines = [
        "# lumin-chat Docker Ubuntu 测试报告",
        "",
        "## 1. 测试环境",
        "",
        f"- 生成时间: {now}",
        f"- 目标主机: {user}@{host}:{port}",
        f"- 远端目录: {remote_dir}",
        f"- 通过情况: {passed}/{total}",
        "",
        "## 2. 总结",
        "",
    ]

    if passed == total:
        lines.append("本次远端部署、项目冒烟与 Docker Ubuntu 非交互测试全部通过。")
    else:
        lines.append("本次测试存在失败项，下面给出逐项命令、退出码与输出，便于继续定位。")

    lines.extend(["", "## 3. 详细结果", ""])

    for index, item in enumerate(results, start=1):
        status = "通过" if item["returncode"] == 0 else "失败"
        stdout = sanitize_output(item.get("stdout") or "") or "<empty>"
        stderr = sanitize_output(item.get("stderr") or "")
        lines.extend(
            [
                f"### 3.{index} {item['name']} [{status}]",
                "",
                "命令：",
                "```bash",
                str(item["command"]),
                "```",
                "",
                f"退出码：{item['returncode']}",
                "",
                "标准输出：",
                "```text",
                stdout,
                "```",
            ]
        )
        if stderr:
            lines.extend(["", "标准错误：", "```text", stderr, "```"])
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    """执行远端 Docker Ubuntu 测试并生成 Markdown 报告。"""

    parser = argparse.ArgumentParser(description="执行 lumin-chat 远端 Docker Ubuntu 非交互测试")
    parser.add_argument("--host", default="117.72.194.76")
    parser.add_argument("--port", type=int, default=3568)
    parser.add_argument("--user", default="root")
    parser.add_argument("--remote-dir", default="/var/lib/lumin-chat")
    parser.add_argument("--launcher", default="/usr/bin/lumin-chat")
    parser.add_argument("--config-path", default="/etc/lumin-chat/config.json")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args()

    results: list[dict[str, object]] = []
    for test_case in build_test_cases(args.remote_dir, args.launcher, args.config_path):
        completed = remote_run(args.host, args.port, args.user, test_case["command"])
        results.append(
            {
                "name": test_case["name"],
                "command": test_case["command"],
                "returncode": completed.returncode,
                "stdout": sanitize_output(completed.stdout),
                "stderr": sanitize_output(completed.stderr),
            }
        )

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_text = render_report(args.host, args.port, args.user, args.remote_dir, results)
    report_path.write_text(report_text, encoding="utf-8")

    print(json.dumps({
        "report": str(report_path),
        "passed": sum(1 for item in results if item["returncode"] == 0),
        "total": len(results),
    }, ensure_ascii=False))
    return 0 if all(item["returncode"] == 0 for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())