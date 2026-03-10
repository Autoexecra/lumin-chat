"""lumin-chat 一键构建、RPM 打包、部署与远端测试脚本。"""

from __future__ import annotations

import argparse
import getpass
import json
import posixpath
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Dict, List

from scripts.docker_ubuntu_test import build_test_cases, render_report, sanitize_output
from src.config_loader import load_config
from src.ssh_client import SSHConnectionConfig, SSHRemoteClient


APP_INSTALL_DIR = "/var/lib/lumin-chat"
SYSTEM_CONFIG_PATH = "/etc/lumin-chat/config.json"
LAUNCHER_PATH = "/usr/bin/lumin-chat"


def stage_project(project_root: Path, stage_root: Path) -> None:
    """准备远端构建和源码部署使用的最小文件集。"""

    if stage_root.exists():
        shutil.rmtree(stage_root)
    stage_root.mkdir(parents=True, exist_ok=True)

    for file_name in ["main.py", "config.json", "requirements.txt", "deploy.py", "README.md"]:
        shutil.copy2(project_root / file_name, stage_root / file_name)

    for folder_name in ["docs", "scripts", "src"]:
        source_dir = project_root / folder_name
        if not source_dir.exists():
            continue
        target_dir = stage_root / folder_name
        target_dir.mkdir(parents=True, exist_ok=True)
        for path in source_dir.rglob("*"):
            if any(part == "__pycache__" for part in path.parts):
                continue
            if path.is_dir():
                (target_dir / path.relative_to(source_dir)).mkdir(parents=True, exist_ok=True)
                continue
            if path.suffix.lower() not in {".py", ".md", ".json", ".sh", ".ps1", ".txt"}:
                continue
            destination = target_dir / path.relative_to(source_dir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".sh":
                destination.write_text(path.read_text(encoding="utf-8").replace("\r\n", "\n"), encoding="utf-8", newline="\n")
            else:
                shutil.copy2(path, destination)


def create_archive(stage_root: Path, archive_path: Path) -> None:
    """将暂存目录打包为 tar.gz。"""

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        archive_path.unlink()
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(stage_root, arcname=".")


def make_connection(host: str, port: int, user: str, password: str = "") -> SSHConnectionConfig:
    """构造 SSH 连接配置。"""

    return SSHConnectionConfig(host=host, port=port, username=user, password=password)


def _looks_like_ip(host: str) -> bool:
    """粗略判断目标是否为 IP 地址。"""

    return bool(re.fullmatch(r"[0-9.]+", host or ""))


def resolve_target_config(config: Dict, args: argparse.Namespace) -> Dict[str, object]:
    """解析目标测试板连接参数。"""

    deploy_config = config.get("deploy", {})
    host = args.host or deploy_config.get("host", "117.72.194.76")
    port = args.port or int(deploy_config.get("port", 3568))
    password = args.password or ""
    if args.user is not None:
        user = args.user
    elif args.host and port == 22 and not password and not _looks_like_ip(str(host)):
        user = ""
    else:
        user = deploy_config.get("user", getpass.getuser())
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "remote_dir": args.remote_dir or deploy_config.get("remote_dir", "/root/lumin-chat"),
    }


def resolve_build_config(config: Dict, args: argparse.Namespace) -> Dict[str, object]:
    """解析远端构建服务器连接参数。"""

    build_config = config.get("build_server", {})
    cli_enabled = any([args.build_host, args.build_password, args.build_user is not None, args.build_remote_dir])
    enabled = bool(args.use_build_server or cli_enabled or build_config.get("enabled", False))
    host = args.build_host or build_config.get("host", "")
    port = args.build_port or int(build_config.get("port", 22))
    password = args.build_password if args.build_password is not None else build_config.get("password", "")
    if args.build_user is not None:
        user = args.build_user
    elif args.build_host and port == 22 and not password and not _looks_like_ip(str(host)):
        user = ""
    else:
        user = build_config.get("user", getpass.getuser())
    return {
        "enabled": enabled,
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "remote_dir": args.build_remote_dir or build_config.get("remote_dir", "/root/lumin-chat-build"),
    }


def shlex_quote(value: str) -> str:
    """对远端 shell 参数进行转义。"""

    import shlex

    return shlex.quote(value)


def _ssh_target(connection: SSHConnectionConfig) -> str:
    """格式化 ssh/scp 目标。"""

    return f"{connection.username}@{connection.host}" if connection.username else connection.host


def run_ssh_cli(
    connection: SSHConnectionConfig,
    command: str,
    cwd: str | None = None,
    timeout_seconds: int = 60,
) -> subprocess.CompletedProcess[str]:
    """使用系统 ssh 命令执行远端命令。"""

    remote_command = command if not cwd else f"cd {shlex_quote(cwd)} && {command}"
    return subprocess.run(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-p",
            str(connection.port),
            _ssh_target(connection),
            remote_command,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )


def run_ssh_cli_with_retry(
    connection: SSHConnectionConfig,
    command: str,
    cwd: str | None = None,
    timeout_seconds: int = 60,
    attempts: int = 4,
) -> subprocess.CompletedProcess[str]:
    """执行远端命令，并对偶发 SSH 传输错误进行重试。"""

    last_result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, max(1, attempts) + 1):
        last_result = run_ssh_cli(connection, command, cwd=cwd, timeout_seconds=timeout_seconds)
        stderr = (last_result.stderr or "") + "\n" + (last_result.stdout or "")
        if last_result.returncode == 0:
            return last_result
        if not _looks_like_transient_ssh_error(stderr):
            return last_result
        if attempt < attempts:
            time.sleep(min(attempt, 3))
    return last_result if last_result is not None else run_ssh_cli(connection, command, cwd=cwd, timeout_seconds=timeout_seconds)


def run_ssh_cli_checked(
    connection: SSHConnectionConfig,
    command: str,
    cwd: str | None = None,
    timeout_seconds: int = 60,
    attempts: int = 4,
    error_message: str = "远端命令执行失败",
) -> subprocess.CompletedProcess[str]:
    """执行带重试的远端命令，并在失败时抛出明确异常。"""

    completed = run_ssh_cli_with_retry(
        connection,
        command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        attempts=attempts,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{error_message}: {completed.stderr or completed.stdout}")
    return completed


def _looks_like_transient_ssh_error(output: str) -> bool:
    """判断是否为可重试的 SSH 传输层异常。"""

    markers = (
        "Bad packet length",
        "Connection corrupted",
        "Connection closed by UNKNOWN port 65535",
        "Connection reset by peer",
        "Broken pipe",
    )
    return any(marker in output for marker in markers)


def upload_file_cli(connection: SSHConnectionConfig, local_path: Path, remote_path: str) -> None:
    """使用系统 scp 上传单个文件。"""

    command = [
        "scp",
        "-O",
        "-o",
        "StrictHostKeyChecking=no",
        "-P",
        str(connection.port),
        str(local_path),
        f"{_ssh_target(connection)}:{remote_path}",
    ]
    _run_scp_with_retry(command)


def download_file_cli(connection: SSHConnectionConfig, remote_path: str, local_path: Path) -> None:
    """使用系统 scp 下载单个文件。"""

    local_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "scp",
        "-O",
        "-o",
        "StrictHostKeyChecking=no",
        "-P",
        str(connection.port),
        f"{_ssh_target(connection)}:{remote_path}",
        str(local_path),
    ]
    _run_scp_with_retry(command)


def _run_scp_with_retry(command: list[str], attempts: int = 4) -> None:
    """执行 scp 并对传输层抖动进行重试。"""

    last_error: subprocess.CalledProcessError | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            subprocess.run(command, check=True)
            return
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if attempt >= attempts:
                break
            time.sleep(min(attempt, 3))
    if last_error is not None:
        raise last_error


def build_archive_on_server(stage_root: Path, build_config: Dict[str, object], local_archive: Path) -> None:
    """在远端构建机生成源码部署包。"""

    remote_dir = str(build_config["remote_dir"])
    source_dir = posixpath.join(remote_dir, "source")
    artifact_path = posixpath.join(remote_dir, "lumin-chat.tar.gz")
    source_archive = local_archive.parent / "build-source.tar.gz"
    create_archive(stage_root, source_archive)
    connection = make_connection(
        host=str(build_config["host"]),
        port=int(build_config["port"]),
        user=str(build_config["user"]),
        password=str(build_config["password"]),
    )

    if not str(build_config["password"]):
        remote_source_archive = posixpath.join(remote_dir, "build-source.tar.gz")
        run_ssh_cli_checked(
            connection,
            f"rm -rf {shlex_quote(remote_dir)} && mkdir -p {shlex_quote(source_dir)}",
            timeout_seconds=300,
            error_message="远端构建目录准备失败",
        )
        upload_file_cli(connection, source_archive, remote_source_archive)
        commands = [
            f"tar -xzf {shlex_quote(remote_source_archive)} -C {shlex_quote(source_dir)}",
            "python3 -m compileall main.py src scripts",
            f"tar -czf {shlex_quote(artifact_path)} -C {shlex_quote(source_dir)} .",
        ]
        for command in commands:
            run_ssh_cli_checked(
                connection,
                command,
                cwd=source_dir,
                timeout_seconds=300,
                error_message="远端源码包构建失败",
            )
        download_file_cli(connection, artifact_path, local_archive)
        return

    with SSHRemoteClient(connection) as client:
        client.remove_remote_path(remote_dir)
        client.ensure_remote_dir(source_dir)
        remote_source_archive = posixpath.join(remote_dir, "build-source.tar.gz")
        client.upload_file(source_archive, remote_source_archive)
        for command in [
            f"tar -xzf {shlex_quote(remote_source_archive)} -C {shlex_quote(source_dir)}",
            "python3 -m compileall main.py src scripts",
            f"tar -czf {shlex_quote(artifact_path)} -C {shlex_quote(source_dir)} .",
        ]:
            completed = client.run(command=command, cwd=source_dir, timeout_seconds=300)
            if int(completed["exit_code"]) != 0:
                raise RuntimeError(str(completed.get("stderr") or completed.get("stdout") or "远端构建失败"))
        client.download_file(artifact_path, local_archive)


def build_rpm_locally(project_root: Path, output_dir: Path) -> Path:
    """在本地构建 RPM 包。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "python3",
            str(project_root / "scripts" / "build_rpm.py"),
            "--output-dir",
            str(output_dir),
        ],
        check=True,
        cwd=project_root,
    )
    built_rpms = sorted(output_dir.glob("*.rpm"))
    if not built_rpms:
        raise FileNotFoundError("本地 RPM 构建完成后未找到 rpm 文件")
    return built_rpms[-1]


def build_rpm_on_server(stage_root: Path, build_config: Dict[str, object], output_dir: Path) -> Path:
    """在远端构建服务器构建 RPM，并将产物下载回本地。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    remote_dir = str(build_config["remote_dir"])
    source_dir = posixpath.join(remote_dir, "source")
    remote_output_dir = posixpath.join(remote_dir, "output")
    source_archive = output_dir / "build-rpm-source.tar.gz"
    create_archive(stage_root, source_archive)
    connection = make_connection(
        host=str(build_config["host"]),
        port=int(build_config["port"]),
        user=str(build_config["user"]),
        password=str(build_config["password"]),
    )

    remote_source_archive = posixpath.join(remote_dir, "build-source.tar.gz")
    if not str(build_config["password"]):
        run_ssh_cli_checked(
            connection,
            f"rm -rf {shlex_quote(remote_dir)} && mkdir -p {shlex_quote(source_dir)} {shlex_quote(remote_output_dir)}",
            timeout_seconds=300,
            error_message="远端 RPM 构建目录准备失败",
        )
        upload_file_cli(connection, source_archive, remote_source_archive)
        commands = [
            f"tar -xzf {shlex_quote(remote_source_archive)} -C {shlex_quote(source_dir)}",
            "python3 -m compileall main.py src scripts",
            f"python3 scripts/build_rpm.py --output-dir {shlex_quote(remote_output_dir)} --work-dir {shlex_quote(posixpath.join(remote_dir, 'rpmbuild'))}",
        ]
        for command in commands:
            run_ssh_cli_checked(
                connection,
                command,
                cwd=source_dir,
                timeout_seconds=600,
                error_message="远端 RPM 构建失败",
            )
        query = run_ssh_cli_checked(
            connection,
            f"ls -1 {shlex_quote(remote_output_dir)}/*.rpm | tail -n 1",
            timeout_seconds=120,
            error_message="未找到远端 RPM 包",
        )
        rpm_path = query.stdout.strip().splitlines()[-1]
        local_rpm = output_dir / Path(rpm_path).name
        download_file_cli(connection, rpm_path, local_rpm)
        return local_rpm

    with SSHRemoteClient(connection) as client:
        client.remove_remote_path(remote_dir)
        client.ensure_remote_dir(source_dir)
        client.ensure_remote_dir(remote_output_dir)
        client.upload_file(source_archive, remote_source_archive)
        for command in [
            f"tar -xzf {shlex_quote(remote_source_archive)} -C {shlex_quote(source_dir)}",
            "python3 -m compileall main.py src scripts",
            f"python3 scripts/build_rpm.py --output-dir {shlex_quote(remote_output_dir)} --work-dir {shlex_quote(posixpath.join(remote_dir, 'rpmbuild'))}",
        ]:
            completed = client.run(command=command, cwd=source_dir, timeout_seconds=600)
            if int(completed["exit_code"]) != 0:
                raise RuntimeError(str(completed.get("stderr") or completed.get("stdout") or "远端 RPM 构建失败"))
        query = client.run(command=f"ls -1 {shlex_quote(remote_output_dir)}/*.rpm | tail -n 1", timeout_seconds=120)
        if int(query["exit_code"]) != 0:
            raise RuntimeError(str(query.get("stderr") or query.get("stdout") or "未找到远端 RPM 包"))
        rpm_path = str(query.get("stdout", "")).strip().splitlines()[-1]
        local_rpm = output_dir / Path(rpm_path).name
        client.download_file(rpm_path, local_rpm)
        return local_rpm


def deploy_archive_to_target(archive_path: Path, target_config: Dict[str, object], bootstrap: bool) -> None:
    """将源码部署包上传到目标机并解压，可选执行初始化。"""

    remote_dir = str(target_config["remote_dir"])
    remote_archive = posixpath.join(remote_dir, "lumin-chat.tar.gz")
    connection = make_connection(
        host=str(target_config["host"]),
        port=int(target_config["port"]),
        user=str(target_config["user"]),
        password=str(target_config["password"]),
    )
    if not str(target_config["password"]):
        run_ssh_cli_checked(
            connection,
            f"mkdir -p {shlex_quote(remote_dir)}",
            timeout_seconds=120,
            error_message="测试板部署目录准备失败",
        )
        upload_file_cli(connection, archive_path, remote_archive)
        unpack_result = run_ssh_cli_with_retry(
            connection,
            (
                f"find {shlex_quote(remote_dir)} -mindepth 1 -maxdepth 1 ! -name 'lumin-chat.tar.gz' -exec rm -rf {{}} + && "
                f"tar -xzf {shlex_quote(remote_archive)} -C {shlex_quote(remote_dir)}"
            ),
            timeout_seconds=300,
        )
        if unpack_result.returncode != 0:
            raise RuntimeError(f"测试板解压失败: {unpack_result.stderr or unpack_result.stdout}")
        if bootstrap:
            bootstrap_result = run_ssh_cli_with_retry(
                connection,
                f"bash scripts/remote_bootstrap.sh {shlex_quote(remote_dir)}",
                cwd=remote_dir,
                timeout_seconds=600,
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
            timeout_seconds=300,
        )
        if int(unpack_result["exit_code"]) != 0:
            raise RuntimeError(f"测试板解压失败: {unpack_result['stderr'] or unpack_result['stdout']}")
        if bootstrap:
            bootstrap_result = client.run(
                command=f"bash scripts/remote_bootstrap.sh {shlex_quote(remote_dir)}",
                cwd=remote_dir,
                timeout_seconds=600,
            )
            if int(bootstrap_result["exit_code"]) != 0:
                raise RuntimeError(f"测试板初始化失败: {bootstrap_result['stderr'] or bootstrap_result['stdout']}")


def install_rpm_to_target(rpm_path: Path, target_config: Dict[str, object]) -> None:
    """上传 RPM 到目标机并安装。"""

    remote_dir = str(target_config["remote_dir"])
    remote_rpm = posixpath.join(remote_dir, rpm_path.name)
    connection = make_connection(
        host=str(target_config["host"]),
        port=int(target_config["port"]),
        user=str(target_config["user"]),
        password=str(target_config["password"]),
    )
    install_command = f"mkdir -p {shlex_quote(remote_dir)} && rpm -Uvh --replacepkgs --force {shlex_quote(remote_rpm)}"

    if not str(target_config["password"]):
        run_ssh_cli_checked(
            connection,
            f"mkdir -p {shlex_quote(remote_dir)}",
            timeout_seconds=120,
            error_message="测试板 RPM 目录准备失败",
        )
        upload_file_cli(connection, rpm_path, remote_rpm)
        completed = run_ssh_cli_with_retry(connection, install_command, timeout_seconds=1800)
        if completed.returncode != 0:
            if _rpm_install_verified(connection):
                return
            raise RuntimeError(f"RPM 安装失败: {completed.stderr or completed.stdout}")
        return

    with SSHRemoteClient(connection) as client:
        client.ensure_remote_dir(remote_dir)
        client.upload_file(rpm_path, remote_rpm)
        completed = client.run(install_command, timeout_seconds=1800)
        if int(completed["exit_code"]) != 0:
            check = client.run("rpm -q lumin-chat", timeout_seconds=60)
            if int(check["exit_code"]) == 0:
                return
            raise RuntimeError(f"RPM 安装失败: {completed['stderr'] or completed['stdout']}")


def _rpm_install_verified(connection: SSHConnectionConfig) -> bool:
    """在 SSH CLI 场景下回查 RPM 是否已安装成功。"""

    try:
        check = run_ssh_cli_with_retry(connection, "rpm -q lumin-chat", timeout_seconds=120)
    except Exception:
        return False
    return check.returncode == 0


def run_remote_validation(target_config: Dict[str, object], report_path: Path) -> Dict[str, object]:
    """在目标机执行安装验证、项目冒烟与 Docker Ubuntu 非交互测试。"""

    results: List[Dict[str, object]] = []
    connection = make_connection(
        host=str(target_config["host"]),
        port=int(target_config["port"]),
        user=str(target_config["user"]),
        password=str(target_config["password"]),
    )
    if not str(target_config["password"]):
        for test_case in build_test_cases(APP_INSTALL_DIR, LAUNCHER_PATH, SYSTEM_CONFIG_PATH):
            completed = run_ssh_cli_with_retry(connection, test_case["command"], timeout_seconds=600)
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
            for test_case in build_test_cases(APP_INSTALL_DIR, LAUNCHER_PATH, SYSTEM_CONFIG_PATH):
                completed = client.run(test_case["command"], timeout_seconds=600)
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
        user=str(target_config["user"] or getpass.getuser()),
        remote_dir=APP_INSTALL_DIR,
        results=results,
    )
    report_path.write_text(report_text, encoding="utf-8")
    return {
        "report": str(report_path),
        "passed": sum(1 for item in results if item["returncode"] == 0),
        "total": len(results),
    }


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""

    parser = argparse.ArgumentParser(description="部署 lumin-chat 到远端测试板，并支持远端构建服务器与 RPM 打包")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--host", help="测试板主机")
    parser.add_argument("--port", type=int, help="测试板 SSH 端口")
    parser.add_argument("--user", help="测试板用户名；若为空且 host 为 SSH alias，则沿用 alias 配置")
    parser.add_argument("--password", default="", help="测试板密码，默认空表示优先使用密钥")
    parser.add_argument("--remote-dir", help="测试板临时上传目录")
    parser.add_argument("--bootstrap", action="store_true", help="仅源码部署模式下，在测试板创建虚拟环境并安装依赖")
    parser.add_argument("--run-tests", action="store_true", help="部署后自动执行远端冒烟与 Docker Ubuntu 测试")
    parser.add_argument("--report", default="reports/docker_ubuntu_test_report.md", help="测试报告输出路径")
    parser.add_argument("--use-build-server", action="store_true", help="启用远端构建服务器流程")
    parser.add_argument("--build-host", help="构建服务器主机")
    parser.add_argument("--build-port", type=int, help="构建服务器 SSH 端口")
    parser.add_argument("--build-user", help="构建服务器用户名；为空且 host 为 SSH alias 时沿用 alias 配置")
    parser.add_argument("--build-password", help="构建服务器密码")
    parser.add_argument("--build-remote-dir", help="构建服务器工作目录")
    parser.add_argument("--package-format", choices=["rpm", "source"], default="rpm", help="部署产物格式，默认 rpm")
    return parser


def main() -> int:
    """执行构建、部署和测试流程。"""

    parser = build_parser()
    args = parser.parse_args()

    config = load_config(args.config)
    project_root = Path(__file__).resolve().parent
    stage_root = project_root / ".dist" / "deploy-package"
    archive_path = project_root / ".dist" / "artifacts" / "lumin-chat.tar.gz"
    rpm_output_dir = project_root / ".dist" / "rpm"
    report_path = (project_root / args.report).resolve() if not Path(args.report).is_absolute() else Path(args.report)

    stage_project(project_root, stage_root)
    target_config = resolve_target_config(config, args)
    build_config = resolve_build_config(config, args)

    summary = {
        "host": target_config["host"],
        "port": target_config["port"],
        "upload_dir": target_config["remote_dir"],
        "package_format": args.package_format,
        "bootstrap": bool(args.bootstrap),
        "tests_ran": False,
    }

    if args.package_format == "rpm":
        if build_config["enabled"]:
            rpm_path = build_rpm_on_server(stage_root, build_config, rpm_output_dir)
        else:
            rpm_path = build_rpm_locally(project_root, rpm_output_dir)
        install_rpm_to_target(rpm_path, target_config)
        summary["artifact"] = str(rpm_path)
        summary["installed_to"] = APP_INSTALL_DIR
    else:
        if build_config["enabled"]:
            build_archive_on_server(stage_root, build_config, archive_path)
        else:
            create_archive(stage_root, archive_path)
        deploy_archive_to_target(archive_path, target_config, bootstrap=args.bootstrap)
        summary["artifact"] = str(archive_path)
        summary["installed_to"] = str(target_config["remote_dir"])

    if args.run_tests:
        summary.update(run_remote_validation(target_config, report_path))
        summary["tests_ran"] = True

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
