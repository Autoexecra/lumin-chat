"""lumin-chat 一键部署与远端构建脚本。"""

from __future__ import annotations

import argparse
import json
import posixpath
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Dict, List

from scripts.docker_ubuntu_test import build_test_cases, render_report, sanitize_output
from src.config_loader import load_config
from src.ssh_client import SSHConnectionConfig, SSHRemoteClient


def stage_project(project_root: Path, stage_root: Path) -> None:
    """准备最小部署包，避免把缓存、虚拟环境和临时文件上传到远端。"""

    if stage_root.exists():
        shutil.rmtree(stage_root)
    (stage_root / "src").mkdir(parents=True, exist_ok=True)
    (stage_root / "docs").mkdir(parents=True, exist_ok=True)
    (stage_root / "scripts").mkdir(parents=True, exist_ok=True)
    (stage_root / "reports").mkdir(parents=True, exist_ok=True)

    for file_name in ["main.py", "config.json", "requirements.txt", "deploy.py"]:
        shutil.copy2(project_root / file_name, stage_root / file_name)

    for path in (project_root / "docs").glob("*.md"):
        shutil.copy2(path, stage_root / "docs" / path.name)

    for path in (project_root / "reports").glob("*.md"):
        shutil.copy2(path, stage_root / "reports" / path.name)

    for script_name in ["remote_bootstrap.sh", "smoke_test.py", "docker_ubuntu_test.py"]:
        source_path = project_root / "scripts" / script_name
        target_path = stage_root / "scripts" / script_name
        if source_path.suffix == ".sh":
            target_path.write_text(source_path.read_text(encoding="utf-8").replace("\r\n", "\n"), encoding="utf-8", newline="\n")
        else:
            shutil.copy2(source_path, target_path)

    for path in (project_root / "src").glob("*.py"):
        shutil.copy2(path, stage_root / "src" / path.name)


def create_archive(stage_root: Path, archive_path: Path) -> None:
    """把准备好的目录打成 tar.gz，便于在远端上传与解压。"""

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        archive_path.unlink()
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(stage_root, arcname=".")


def make_connection(host: str, port: int, user: str, password: str = "") -> SSHConnectionConfig:
    """构造 SSH 连接配置。"""

    return SSHConnectionConfig(host=host, port=port, username=user, password=password)


def resolve_target_config(config: Dict, args: argparse.Namespace) -> Dict[str, object]:
    """解析目标测试板连接参数。"""

    deploy_config = config.get("deploy", {})
    return {
        "host": args.host or deploy_config.get("host", "117.72.194.76"),
        "port": args.port or int(deploy_config.get("port", 3568)),
        "user": args.user or deploy_config.get("user", "root"),
        "password": args.password or "",
        "remote_dir": args.remote_dir or deploy_config.get("remote_dir", "/root/lumin-chat"),
    }


def resolve_build_config(config: Dict, args: argparse.Namespace) -> Dict[str, object]:
    """解析远端构建服务器配置。"""

    build_config = config.get("build_server", {})
    cli_enabled = any([args.build_host, args.build_password, args.build_user, args.build_remote_dir])
    enabled = bool(args.use_build_server or cli_enabled or build_config.get("enabled", False))
    return {
        "enabled": enabled,
        "host": args.build_host or build_config.get("host", ""),
        "port": args.build_port or int(build_config.get("port", 22)),
        "user": args.build_user or build_config.get("user", "root"),
        "password": args.build_password if args.build_password is not None else build_config.get("password", ""),
        "remote_dir": args.build_remote_dir or build_config.get("remote_dir", "/root/lumin-chat-build"),
    }


def build_archive_on_server(stage_root: Path, build_config: Dict[str, object], local_archive: Path) -> None:
    """把源代码上传到构建机，执行 compileall 校验后再产出部署包。"""

    remote_dir = str(build_config["remote_dir"])
    source_dir = posixpath.join(remote_dir, "source")
    artifact_path = posixpath.join(remote_dir, "lumin-chat.tar.gz")
    source_archive = local_archive.parent / "build-source.tar.gz"
    connection = make_connection(
        host=str(build_config["host"]),
        port=int(build_config["port"]),
        user=str(build_config["user"]),
        password=str(build_config["password"]),
    )
    create_archive(stage_root, source_archive)

    if not str(build_config["password"]):
        remote_source_archive = posixpath.join(remote_dir, "build-source.tar.gz")
        run_ssh_cli(connection, f"rm -rf {shlex_quote(remote_dir)} && mkdir -p {shlex_quote(source_dir)}")
        upload_file_cli(connection, source_archive, remote_source_archive)
        extract_result = run_ssh_cli(
            connection,
            f"tar -xzf {shlex_quote(remote_source_archive)} -C {shlex_quote(source_dir)}",
            timeout_seconds=180,
        )
        if extract_result.returncode != 0:
            raise RuntimeError(f"构建服务器解压失败: {extract_result.stderr or extract_result.stdout}")
        compile_result = run_ssh_cli(connection, "python3 -m compileall main.py src scripts", cwd=source_dir, timeout_seconds=180)
        if compile_result.returncode != 0:
            raise RuntimeError(f"构建服务器 compileall 失败: {compile_result.stderr or compile_result.stdout}")
        package_result = run_ssh_cli(
            connection,
            f"tar -czf {shlex_quote(artifact_path)} -C {shlex_quote(source_dir)} .",
            timeout_seconds=180,
        )
        if package_result.returncode != 0:
            raise RuntimeError(f"构建服务器打包失败: {package_result.stderr or package_result.stdout}")
        download_file_cli(connection, artifact_path, local_archive)
        return

    with SSHRemoteClient(connection) as client:
        client.remove_remote_path(remote_dir)
        client.ensure_remote_dir(source_dir)
        client.upload_file(source_archive, posixpath.join(remote_dir, "build-source.tar.gz"))
        extract_result = client.run(
            command=f"tar -xzf {shlex_quote(posixpath.join(remote_dir, 'build-source.tar.gz'))} -C {shlex_quote(source_dir)}",
            timeout_seconds=180,
        )
        if int(extract_result["exit_code"]) != 0:
            raise RuntimeError(f"构建服务器解压失败: {extract_result['stderr'] or extract_result['stdout']}")
        compile_result = client.run(
            command="python3 -m compileall main.py src scripts",
            cwd=source_dir,
            timeout_seconds=180,
        )
        if int(compile_result["exit_code"]) != 0:
            raise RuntimeError(f"构建服务器 compileall 失败: {compile_result['stderr'] or compile_result['stdout']}")
        package_result = client.run(
            command=f"tar -czf {shlex_quote(artifact_path)} -C {shlex_quote(source_dir)} .",
            timeout_seconds=180,
        )
        if int(package_result["exit_code"]) != 0:
            raise RuntimeError(f"构建服务器打包失败: {package_result['stderr'] or package_result['stdout']}")
        client.download_file(artifact_path, local_archive)


def deploy_archive_to_target(archive_path: Path, target_config: Dict[str, object], bootstrap: bool) -> None:
    """将部署包上传到测试板并解压。"""

    remote_dir = str(target_config["remote_dir"])
    remote_archive = posixpath.join(remote_dir, "lumin-chat.tar.gz")
    connection = make_connection(
        host=str(target_config["host"]),
        port=int(target_config["port"]),
        user=str(target_config["user"]),
        password=str(target_config["password"]),
    )
    if not str(target_config["password"]):
        run_ssh_cli(connection, f"mkdir -p {shlex_quote(remote_dir)}")
        upload_file_cli(connection, archive_path, remote_archive)
        unpack_result = run_ssh_cli(
            connection,
            (
                f"find {shlex_quote(remote_dir)} -mindepth 1 -maxdepth 1 ! -name 'lumin-chat.tar.gz' -exec rm -rf {{}} + && "
                f"tar -xzf {shlex_quote(remote_archive)} -C {shlex_quote(remote_dir)}"
            ),
            timeout_seconds=180,
        )
        if unpack_result.returncode != 0:
            raise RuntimeError(f"测试板解压失败: {unpack_result.stderr or unpack_result.stdout}")
        if bootstrap:
            bootstrap_result = run_ssh_cli(
                connection,
                f"bash scripts/remote_bootstrap.sh {shlex_quote(remote_dir)}",
                cwd=remote_dir,
                timeout_seconds=300,
            )
            if bootstrap_result.returncode != 0:
                raise RuntimeError(f"测试板初始化失败: {bootstrap_result.stderr or bootstrap_result.stdout}")
        return

    with SSHRemoteClient(connection) as client:
        client.ensure_remote_dir(remote_dir)
        client.upload_file(archive_path, remote_archive)
        unpack_result = client.run(
            command=(
                f"find {shlex_quote(remote_dir)} -mindepth 1 -maxdepth 1 ! -name 'lumin-chat.tar.gz' -exec rm -rf {{}} + && "
                f"tar -xzf {shlex_quote(remote_archive)} -C {shlex_quote(remote_dir)}"
            ),
            timeout_seconds=180,
        )
        if int(unpack_result["exit_code"]) != 0:
            raise RuntimeError(f"测试板解压失败: {unpack_result['stderr'] or unpack_result['stdout']}")
        if bootstrap:
            bootstrap_result = client.run(
                command=f"bash scripts/remote_bootstrap.sh {shlex_quote(remote_dir)}",
                cwd=remote_dir,
                timeout_seconds=300,
            )
            if int(bootstrap_result["exit_code"]) != 0:
                raise RuntimeError(f"测试板初始化失败: {bootstrap_result['stderr'] or bootstrap_result['stdout']}")


def run_remote_validation(target_config: Dict[str, object], report_path: Path) -> Dict[str, object]:
    """在测试板上执行项目冒烟与 Docker Ubuntu 验证，并生成中文报告。"""

    results: List[Dict[str, object]] = []
    remote_dir = str(target_config["remote_dir"])
    connection = make_connection(
        host=str(target_config["host"]),
        port=int(target_config["port"]),
        user=str(target_config["user"]),
        password=str(target_config["password"]),
    )
    if not str(target_config["password"]):
        for test_case in build_test_cases(remote_dir):
            completed = run_ssh_cli(connection, test_case["command"], timeout_seconds=180)
            results.append(
                {
                    "name": test_case["name"],
                    "command": test_case["command"],
                    "returncode": completed.returncode,
                    "stdout": sanitize_output(completed.stdout),
                    "stderr": sanitize_output(completed.stderr),
                }
            )
    else:
        with SSHRemoteClient(connection) as client:
            for test_case in build_test_cases(remote_dir):
                completed = client.run(test_case["command"], timeout_seconds=180)
                results.append(
                    {
                        "name": test_case["name"],
                        "command": test_case["command"],
                        "returncode": int(completed["exit_code"]),
                        "stdout": sanitize_output(str(completed.get("stdout", ""))),
                        "stderr": sanitize_output(str(completed.get("stderr", ""))),
                    }
                )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_text = render_report(
        host=str(target_config["host"]),
        port=int(target_config["port"]),
        user=str(target_config["user"]),
        remote_dir=remote_dir,
        results=results,
    )
    report_path.write_text(report_text, encoding="utf-8")
    return {
        "report": str(report_path),
        "passed": sum(1 for item in results if item["returncode"] == 0),
        "total": len(results),
    }


def shlex_quote(value: str) -> str:
    """对远端 shell 参数进行转义。"""

    import shlex

    return shlex.quote(value)


def run_ssh_cli(connection: SSHConnectionConfig, command: str, cwd: str | None = None, timeout_seconds: int = 60) -> subprocess.CompletedProcess[str]:
    """使用系统 ssh 命令执行远端命令。"""

    remote_command = command if not cwd else f"cd {shlex_quote(cwd)} && {command}"
    return subprocess.run(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-p",
            str(connection.port),
            f"{connection.username}@{connection.host}",
            remote_command,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )


def upload_file_cli(connection: SSHConnectionConfig, local_path: Path, remote_path: str) -> None:
    """使用系统 scp 上传单个文件。"""

    subprocess.run(
        [
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-P",
            str(connection.port),
            str(local_path),
            f"{connection.username}@{connection.host}:{remote_path}",
        ],
        check=True,
    )


def download_file_cli(connection: SSHConnectionConfig, remote_path: str, local_path: Path) -> None:
    """使用系统 scp 下载单个文件。"""

    local_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-P",
            str(connection.port),
            f"{connection.username}@{connection.host}:{remote_path}",
            str(local_path),
        ],
        check=True,
    )


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""

    parser = argparse.ArgumentParser(description="部署 lumin-chat 到远端测试板，并支持先在构建服务器生成产物")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--host", help="测试板主机")
    parser.add_argument("--port", type=int, help="测试板 SSH 端口")
    parser.add_argument("--user", help="测试板用户名")
    parser.add_argument("--password", default="", help="测试板密码，默认空表示优先使用密钥")
    parser.add_argument("--remote-dir", help="测试板部署目录")
    parser.add_argument("--bootstrap", action="store_true", help="部署后在测试板创建虚拟环境并安装依赖")
    parser.add_argument("--run-tests", action="store_true", help="部署后自动执行远端冒烟与 Docker Ubuntu 测试")
    parser.add_argument("--report", default="reports/docker_ubuntu_test_report.md", help="测试报告输出路径")
    parser.add_argument("--use-build-server", action="store_true", help="启用远端构建服务器流程")
    parser.add_argument("--build-host", help="构建服务器主机")
    parser.add_argument("--build-port", type=int, help="构建服务器 SSH 端口")
    parser.add_argument("--build-user", help="构建服务器用户名")
    parser.add_argument("--build-password", help="构建服务器密码")
    parser.add_argument("--build-remote-dir", help="构建服务器工作目录")
    return parser


def main() -> int:
    """执行构建、部署和测试流程。"""

    parser = build_parser()
    args = parser.parse_args()

    config = load_config(args.config)
    project_root = Path(__file__).resolve().parent
    stage_root = project_root / ".dist" / "deploy-package"
    archive_path = project_root / ".dist" / "artifacts" / "lumin-chat.tar.gz"
    report_path = (project_root / args.report).resolve() if not Path(args.report).is_absolute() else Path(args.report)

    stage_project(project_root, stage_root)
    target_config = resolve_target_config(config, args)
    build_config = resolve_build_config(config, args)

    if build_config["enabled"]:
        build_archive_on_server(stage_root, build_config, archive_path)
    else:
        create_archive(stage_root, archive_path)

    deploy_archive_to_target(archive_path, target_config, bootstrap=args.bootstrap)

    summary = {
        "host": target_config["host"],
        "port": target_config["port"],
        "remote_dir": target_config["remote_dir"],
        "bootstrap": bool(args.bootstrap),
        "tests_ran": False,
    }
    if args.run_tests:
        summary.update(run_remote_validation(target_config, report_path))
        summary["tests_ran"] = True

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
