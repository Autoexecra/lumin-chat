"""lumin-chat 一键部署脚本。"""

import argparse
import shutil
import subprocess
from pathlib import Path


def stage_project(project_root: Path, stage_root: Path) -> None:
    """准备最小部署包，避免把本地缓存和虚拟环境上传到远端。"""

    if stage_root.exists():
        shutil.rmtree(stage_root)
    (stage_root / "src").mkdir(parents=True, exist_ok=True)
    (stage_root / "docs").mkdir(parents=True, exist_ok=True)
    (stage_root / "scripts").mkdir(parents=True, exist_ok=True)

    for file_name in ["main.py", "config.json", "requirements.txt", "deploy.py"]:
        shutil.copy2(project_root / file_name, stage_root / file_name)

    for path in (project_root / "docs").glob("*.md"):
        shutil.copy2(path, stage_root / "docs" / path.name)

    for script_name in ["remote_bootstrap.sh", "smoke_test.py", "docker_ubuntu_test.py"]:
        shutil.copy2(project_root / "scripts" / script_name, stage_root / "scripts" / script_name)

    for path in (project_root / "src").glob("*.py"):
        shutil.copy2(path, stage_root / "src" / path.name)


def run(command: list[str]) -> None:
    """执行外部命令，失败时直接抛错终止部署。"""

    subprocess.run(command, check=True)


def main() -> int:
    """执行部署流程。"""

    parser = argparse.ArgumentParser(description="部署 lumin-chat 到远程 Linux 主机")
    parser.add_argument("--host", default="117.72.194.76")
    parser.add_argument("--port", type=int, default=3568)
    parser.add_argument("--user", default="root")
    parser.add_argument("--remote-dir", default="/root/lumin-chat")
    parser.add_argument("--bootstrap", action="store_true")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent
    stage_root = project_root / ".dist" / "deploy-py"
    stage_project(project_root, stage_root)

    remote = f"{args.user}@{args.host}:{args.remote_dir}/"
    remote_prepare = (
        f"mkdir -p {args.remote_dir} && "
        f"rm -f {args.remote_dir}/main.py {args.remote_dir}/config.json {args.remote_dir}/requirements.txt {args.remote_dir}/deploy.py && "
        f"rm -rf {args.remote_dir}/src {args.remote_dir}/docs {args.remote_dir}/scripts"
    )
    run(["ssh", "-p", str(args.port), f"{args.user}@{args.host}", remote_prepare])
    run([
        "scp",
        "-P",
        str(args.port),
        "-r",
        str(stage_root / "main.py"),
        str(stage_root / "config.json"),
        str(stage_root / "requirements.txt"),
        str(stage_root / "deploy.py"),
        str(stage_root / "src"),
        str(stage_root / "docs"),
        str(stage_root / "scripts"),
        remote,
    ])
    if args.bootstrap:
        run([
            "ssh",
            "-p",
            str(args.port),
            f"{args.user}@{args.host}",
            f"bash {args.remote_dir}/scripts/remote_bootstrap.sh {args.remote_dir} > /tmp/lumin_chat_bootstrap.log 2>&1",
        ])
    print(f"部署完成: {args.remote_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
