# Copyright (c) 2026 Autoexecra
# Licensed under the Apache License, Version 2.0.
# See LICENSE in the project root for license terms.

"""SSH 远程执行与文件传输封装。"""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from typing import Dict, Iterable, List, Optional

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

    def list_directory(self, remote_dir: str, recursive: bool = False, max_entries: int = 200) -> List[Dict[str, object]]:
        """列出远端目录内容。"""

        self.connect()
        entries: List[Dict[str, object]] = []
        sftp = self._client.open_sftp()
        try:
            self._walk_directory(sftp, remote_dir, remote_dir, recursive, max_entries, entries)
        finally:
            sftp.close()
        return entries

    def read_file(self, remote_path: str, start_line: int = 1, end_line: int = 200) -> str:
        """读取远端文本文件指定行范围。"""

        self.connect()
        start_line = max(1, int(start_line))
        end_line = max(start_line, int(end_line))
        sftp = self._client.open_sftp()
        try:
            with sftp.open(remote_path, "r") as handle:
                payload = handle.read()
        finally:
            sftp.close()

        if isinstance(payload, bytes):
            text = payload.decode("utf-8", errors="replace")
        else:
            text = payload
        lines = text.splitlines()
        selected = lines[start_line - 1 : end_line]
        return "\n".join(f"{index}: {line}" for index, line in enumerate(selected, start=start_line))

    def write_file(self, remote_path: str, content: str, append: bool = False) -> None:
        """写入远端文本文件。"""

        self.connect()
        self.ensure_remote_dir(posixpath.dirname(remote_path) or "/")
        sftp = self._client.open_sftp()
        try:
            mode = "a" if append else "w"
            with sftp.open(remote_path, mode) as handle:
                if isinstance(content, str):
                    handle.write(content)
                else:
                    handle.write(str(content))
        finally:
            sftp.close()

    def path_exists(self, remote_path: str) -> bool:
        """检查远端路径是否存在。"""

        self.connect()
        sftp = self._client.open_sftp()
        try:
            try:
                sftp.stat(remote_path)
                return True
            except OSError:
                return False
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

    def _walk_directory(
        self,
        sftp,
        current_path: str,
        root_path: str,
        recursive: bool,
        max_entries: int,
        results: List[Dict[str, object]],
    ) -> None:
        """遍历远端目录。"""

        if len(results) >= max_entries:
            return
        for entry in sftp.listdir_attr(current_path):
            remote_path = posixpath.join(current_path, entry.filename)
            relative_path = posixpath.relpath(remote_path, root_path)
            is_dir = S_ISDIR(entry.st_mode)
            if not (is_dir or S_ISREG(entry.st_mode)):
                continue
            results.append(
                {
                    "path": remote_path,
                    "relative_path": "." if relative_path == "." else relative_path,
                    "type": "dir" if is_dir else "file",
                    "size": None if is_dir else entry.st_size,
                }
            )
            if len(results) >= max_entries:
                return
            if recursive and is_dir:
                self._walk_directory(sftp, remote_path, root_path, recursive, max_entries, results)
                if len(results) >= max_entries:
                    return

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