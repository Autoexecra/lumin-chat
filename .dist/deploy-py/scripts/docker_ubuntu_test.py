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


def build_test_cases(remote_dir: str) -> list[dict[str, str]]:
    """定义本次 Docker Ubuntu 回归测试的命令集合。"""

    return [
        {
            "name": "检查 Docker 版本",
            "command": "docker --version",
        },
        {
            "name": "检查项目帮助信息",
            "command": f"cd {remote_dir} && .venv/bin/python main.py --help",
        },
        {
            "name": "执行项目冒烟测试",
            "command": f"cd {remote_dir} && .venv/bin/python scripts/smoke_test.py",
        },
        {
            "name": "拉取 Ubuntu 镜像",
            "command": "docker pull ubuntu:latest",
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
        stdout = (item.get("stdout") or "").strip() or "<empty>"
        stderr = (item.get("stderr") or "").strip()
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
    parser.add_argument("--remote-dir", default="/root/lumin-chat")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args()

    results: list[dict[str, object]] = []
    for test_case in build_test_cases(args.remote_dir):
        completed = remote_run(args.host, args.port, args.user, test_case["command"])
        results.append(
            {
                "name": test_case["name"],
                "command": test_case["command"],
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
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