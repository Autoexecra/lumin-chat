# Copyright (c) 2026 Autoexecra
# Licensed under the Apache License, Version 2.0.
# See LICENSE in the project root for license terms.

"""构建 lumin-chat RPM 包。"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tarfile
from pathlib import Path


APP_NAME = "lumin-chat"
APP_DIR = "/var/lib/lumin-chat"
CONFIG_DIR = "/etc/lumin-chat"
CONFIG_PATH = f"{CONFIG_DIR}/config.json"
LAUNCHER_PATH = "/usr/bin/lumin-chat"


def copy_project_files(project_root: Path, stage_root: Path) -> None:
    """复制 RPM 打包所需的最小项目文件集。"""

    if stage_root.exists():
        shutil.rmtree(stage_root)
    stage_root.mkdir(parents=True, exist_ok=True)

    for file_name in ["main.py", "config.json", "requirements.txt", "deploy.py", "README.md", "LICENSE"]:
        shutil.copy2(project_root / file_name, stage_root / file_name)

    for folder_name in ["src", "docs", "scripts"]:
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
            if path.suffix.lower() not in {".py", ".md", ".sh", ".ps1", ".json", ".txt"}:
                continue
            destination = target_dir / path.relative_to(source_dir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".sh":
                destination.write_text(path.read_text(encoding="utf-8").replace("\r\n", "\n"), encoding="utf-8", newline="\n")
            else:
                shutil.copy2(path, destination)


def create_source_archive(stage_root: Path, sources_dir: Path, version: str) -> Path:
    """创建 rpmbuild 需要的源码 tar.gz。"""

    source_tree = sources_dir / f"{APP_NAME}-{version}"
    if source_tree.exists():
        shutil.rmtree(source_tree)
    shutil.copytree(stage_root, source_tree)

    archive_path = sources_dir / f"{APP_NAME}-{version}.tar.gz"
    if archive_path.exists():
        archive_path.unlink()
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(source_tree, arcname=source_tree.name)
    return archive_path


def build_spec(version: str, release: str) -> str:
    """生成 RPM spec 内容。"""

    return f"""Name:           {APP_NAME}
Version:        {version}
Release:        {release}%{{?dist}}
Summary:        Lumin Chat Linux 终端代理
License:        Apache-2.0
BuildArch:      noarch
Requires:       /bin/bash, python3
Source0:        %{{name}}-%{{version}}.tar.gz

%description
lumin-chat 是一个面向 Linux 终端的中文友好型智能代理，支持多轮对话、Shell 工具、SSH 远程操作、文档库读取、网页访问与搜索，以及 RPM 部署。

%prep
%setup -q

%build
# Python 项目无需额外编译步骤。

%install
rm -rf %{{buildroot}}
mkdir -p %{{buildroot}}{APP_DIR}
cp -a * %{{buildroot}}{APP_DIR}/
mkdir -p %{{buildroot}}{CONFIG_DIR}
cp config.json %{{buildroot}}{CONFIG_PATH}
mkdir -p %{{buildroot}}/usr/bin
cat > %{{buildroot}}{LAUNCHER_PATH} <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
APP_DIR={APP_DIR}
CONFIG_PATH={CONFIG_PATH}
VENV_PYTHON="$APP_DIR/.venv/bin/python"
ensure_runtime() {{
    update-ca-trust extract >/dev/null 2>&1 || true
    python3 -m ensurepip --upgrade >/dev/null 2>&1 || true
    if python3 -m venv "$APP_DIR/.venv" >/dev/null 2>&1; then
        "$VENV_PYTHON" -m pip install -r "$APP_DIR/requirements.txt"
        return 0
    fi
    mkdir -p "$APP_DIR/vendor"
    python3 -m pip install --target "$APP_DIR/vendor" -r "$APP_DIR/requirements.txt"
}}

check_runtime() {{
    local target_python="$1"
    "$target_python" - <<'PY' >/dev/null 2>&1
import httpx
import openai
import paramiko
import prompt_toolkit
import rich
PY
}}

if [[ -x "$VENV_PYTHON" ]]; then
    if ! check_runtime "$VENV_PYTHON"; then
        ensure_runtime
    fi
    export PYTHONIOENCODING=UTF-8
    export PYTHONUTF8=1
    exec "$VENV_PYTHON" "$APP_DIR/main.py" --config "$CONFIG_PATH" "$@"
fi
export PYTHONIOENCODING=UTF-8
export PYTHONUTF8=1
if [[ -d "$APP_DIR/vendor" ]]; then
    export PYTHONPATH="$APP_DIR/vendor:${{PYTHONPATH:-}}"
    if python3 - <<'PY' >/dev/null 2>&1
import httpx
import openai
import paramiko
import prompt_toolkit
import rich
PY
    then
        exec python3 "$APP_DIR/main.py" --config "$CONFIG_PATH" "$@"
    fi
fi
ensure_runtime
if [[ -x "$VENV_PYTHON" ]]; then
    exec "$VENV_PYTHON" "$APP_DIR/main.py" --config "$CONFIG_PATH" "$@"
fi
export PYTHONPATH="$APP_DIR/vendor:${{PYTHONPATH:-}}"
exec python3 "$APP_DIR/main.py" --config "$CONFIG_PATH" "$@"
EOF
chmod 0755 %{{buildroot}}{LAUNCHER_PATH}
find %{{buildroot}}{APP_DIR} -type d -name '__pycache__' -prune -exec rm -rf {{}} +
find %{{buildroot}}{APP_DIR} -type f -name '*.pyc' -delete

%post
set -e
APP_DIR={APP_DIR}
CONFIG_PATH={CONFIG_PATH}
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONIOENCODING=UTF-8
export PYTHONUTF8=1
update-ca-trust extract >/dev/null 2>&1 || true
python3 -m compileall "$APP_DIR/main.py" "$APP_DIR/src" "$APP_DIR/scripts" >/dev/null 2>&1 || true
python3 -m ensurepip --upgrade >/dev/null 2>&1 || true
if python3 -m venv "$APP_DIR/.venv" >/dev/null 2>&1; then
    "$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
else
    mkdir -p "$APP_DIR/vendor"
    python3 -m pip install --target "$APP_DIR/vendor" -r "$APP_DIR/requirements.txt"
fi
if [ ! -f "$CONFIG_PATH" ]; then
    mkdir -p "$(dirname "$CONFIG_PATH")"
    cp "$APP_DIR/config.json" "$CONFIG_PATH"
fi

%files
%defattr(-,root,root,-)
%license LICENSE
{APP_DIR}
%dir {CONFIG_DIR}
%config(noreplace) {CONFIG_PATH}
{LAUNCHER_PATH}

%changelog
* Tue Mar 10 2026 lumin-chat automation <noreply@lumin-chat.local> - {version}-{release}
- 新增 RPM 打包与安装支持
"""


def run_rpmbuild(spec_path: Path, topdir: Path) -> None:
    """调用 rpmbuild 生成 RPM。"""

    subprocess.run(
        [
            "rpmbuild",
            "-bb",
            str(spec_path),
            "--define",
            f"_topdir {topdir}",
        ],
        check=True,
    )


def main() -> int:
    """执行 RPM 打包流程。"""

    parser = argparse.ArgumentParser(description="构建 lumin-chat RPM 包")
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--release", default="1")
    parser.add_argument("--output-dir", default="dist/rpm")
    parser.add_argument("--work-dir", default=".dist/rpmbuild")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    work_dir = (project_root / args.work_dir).resolve() if not Path(args.work_dir).is_absolute() else Path(args.work_dir)
    output_dir = (project_root / args.output_dir).resolve() if not Path(args.output_dir).is_absolute() else Path(args.output_dir)
    stage_root = work_dir / "stage"
    sources_dir = work_dir / "SOURCES"
    specs_dir = work_dir / "SPECS"
    rpms_dir = work_dir / "RPMS"

    for folder in [work_dir / "BUILD", work_dir / "BUILDROOT", rpms_dir, work_dir / "SRPMS", sources_dir, specs_dir]:
        folder.mkdir(parents=True, exist_ok=True)

    copy_project_files(project_root, stage_root)
    create_source_archive(stage_root, sources_dir, args.version)

    spec_path = specs_dir / f"{APP_NAME}.spec"
    spec_path.write_text(build_spec(args.version, args.release), encoding="utf-8")
    run_rpmbuild(spec_path, work_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    built_rpms = sorted(rpms_dir.rglob("*.rpm"))
    if not built_rpms:
        raise FileNotFoundError("未找到构建产出的 RPM 文件")

    for rpm_path in built_rpms:
        shutil.copy2(rpm_path, output_dir / rpm_path.name)
        print(output_dir / rpm_path.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
