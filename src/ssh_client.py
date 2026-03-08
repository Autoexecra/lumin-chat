"""SSH 远程执行与文件传输封装。"""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

try:
    import paramiko
except ImportError:
    paramiko = None


@dataclass
class SSHConnectionConfig:
    """描述一个 SSH 连接需要的最小参数。"""

    host: str
    port: int = 22
    username: str = "root"
    password: str = ""
    timeout_seconds: int = 15


class SSHRemoteClient:
    """基于 Paramiko 提供远程命令执行与文件传输能力。"""

    def __init__(self, config: SSHConnectionConfig):
        """保存连接配置，但延迟到首次使用时再建立连接。"""

        self.config = config
        self._client = None

    def __enter__(self) -> "SSHRemoteClient":
        """进入上下文时自动建立连接。"""

        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """离开上下文时关闭 SSH 连接。"""

        self.close()

    def connect(self) -> None:
        """建立 SSH 连接。"""

        if self._client is not None:
            return
        if paramiko is None:
            raise RuntimeError("paramiko is not installed")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = {
            "hostname": self.config.host,
            "port": self.config.port,
            "username": self.config.username,
            "timeout": self.config.timeout_seconds,
            "banner_timeout": self.config.timeout_seconds,
            "auth_timeout": self.config.timeout_seconds,
            "look_for_keys": not bool(self.config.password),
            "allow_agent": not bool(self.config.password),
        }
        if self.config.password:
            connect_kwargs["password"] = self.config.password
        client.connect(**connect_kwargs)
        self._client = client

    def close(self) -> None:
        """关闭 SSH 连接。"""

        if self._client is not None:
            self._client.close()
            self._client = None

    def run(
        self,
        command: str,
        timeout_seconds: int = 60,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, object]:
        """执行远端命令并返回标准化结果。"""

        self.connect()
        remote_command = self._wrap_command(command=command, cwd=cwd, env=env)
        stdin, stdout, stderr = self._client.exec_command(remote_command, timeout=timeout_seconds)
        del stdin
        channel = stdout.channel
        channel.settimeout(timeout_seconds)
        exit_code = channel.recv_exit_status()
        stdout_text = stdout.read().decode("utf-8", errors="replace")
        stderr_text = stderr.read().decode("utf-8", errors="replace")
        return {
            "command": command,
            "cwd": cwd or "",
            "exit_code": exit_code,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "host": self.config.host,
            "port": self.config.port,
            "username": self.config.username,
        }

    def ensure_remote_dir(self, remote_dir: str) -> None:
        """递归创建远端目录。"""

        self.connect()
        path = remote_dir.strip()
        if not path or path == "/":
            return
        sftp = self._client.open_sftp()
        try:
            segments = []
            for part in path.split("/"):
                if not part:
                    continue
                segments.append(part)
                current = "/" + "/".join(segments)
                try:
                    sftp.stat(current)
                except OSError:
                    sftp.mkdir(current)
        finally:
            sftp.close()

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        """上传单个文件到远端。"""

        self.connect()
        self.ensure_remote_dir(posixpath.dirname(remote_path) or "/")
        sftp = self._client.open_sftp()
        try:
            sftp.put(str(local_path), remote_path)
        finally:
            sftp.close()

    def download_file(self, remote_path: str, local_path: Path) -> None:
        """从远端下载单个文件。"""

        self.connect()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        sftp = self._client.open_sftp()
        try:
            sftp.get(remote_path, str(local_path))
        finally:
            sftp.close()

    def upload_tree(self, local_root: Path, remote_root: str, exclude_names: Optional[Iterable[str]] = None) -> None:
        """递归上传目录树，自动跳过缓存与无关产物。"""

        excluded = set(exclude_names or [])
        self.ensure_remote_dir(remote_root)
        for path in local_root.rglob("*"):
            relative = path.relative_to(local_root)
            if any(part in excluded for part in relative.parts):
                continue
            remote_path = posixpath.join(remote_root, relative.as_posix())
            if path.is_dir():
                self.ensure_remote_dir(remote_path)
                continue
            self.upload_file(path, remote_path)

    def remove_remote_path(self, remote_path: str) -> Dict[str, object]:
        """删除远端路径。"""

        return self.run(f"rm -rf {shlex.quote(remote_path)}")

    @staticmethod
    def _wrap_command(command: str, cwd: Optional[str], env: Optional[Dict[str, str]]) -> str:
        """把工作目录和环境变量包装进 bash -lc 命令。"""

        parts = []
        if cwd:
            parts.append(f"cd {shlex.quote(cwd)}")
        for key, value in (env or {}).items():
            parts.append(f"export {key}={shlex.quote(value)}")
        parts.append(command)
        joined = " && ".join(parts)
        return f"bash -lc {shlex.quote(joined)}"