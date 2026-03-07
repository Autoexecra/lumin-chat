"""远程文档库访问能力。"""

import fnmatch
import posixpath
from dataclasses import dataclass
from stat import S_ISDIR, S_ISREG
from typing import Dict, List, Optional

try:
    import paramiko
except ImportError:
    paramiko = None


@dataclass
class KnowledgeBaseConfig:
    """文档库连接配置。"""

    enabled: bool
    host: str
    port: int
    username: str
    password: str
    root_dir: str
    patterns: List[str]


class KnowledgeBaseClient:
    """通过 SFTP 读取远程文档库中的文档。"""

    def __init__(self, config: Dict):
        """从总配置中提取文档库配置。"""

        kb = config.get("knowledge_base", {})
        self.config = KnowledgeBaseConfig(
            enabled=bool(kb.get("enabled", False)),
            host=kb.get("host", ""),
            port=int(kb.get("port", 22)),
            username=kb.get("username", ""),
            password=kb.get("password", ""),
            root_dir=kb.get("root_dir", ""),
            patterns=kb.get("patterns", ["*.md", "*.txt"]),
        )

    @property
    def enabled(self) -> bool:
        """返回文档库是否启用。"""

        return self.config.enabled

    def describe(self) -> Dict[str, object]:
        """返回文档库当前状态。"""

        return {
            "enabled": self.config.enabled,
            "host": self.config.host,
            "port": self.config.port,
            "username": self.config.username,
            "root_dir": self.config.root_dir,
            "patterns": self.config.patterns,
            "available": self.config.enabled and paramiko is not None,
        }

    def list_documents(self, keyword: str = "", limit: int = 50) -> List[Dict[str, object]]:
        """列出与关键字匹配的远程文档名称。"""

        keyword_lower = keyword.lower().strip()
        with self._open_sftp() as sftp:
            results: List[Dict[str, object]] = []
            self._walk(sftp, self.config.root_dir, "", results, keyword_lower, limit)
            return results

    def read_document(self, path: str, start_line: int = 1, end_line: int = 200) -> str:
        """读取远程文档的指定行范围。"""

        normalized = path.strip().lstrip("/")
        full_path = posixpath.join(self.config.root_dir, normalized)
        start_line = max(1, int(start_line))
        end_line = max(start_line, int(end_line))

        with self._open_sftp() as sftp:
            with sftp.open(full_path, "r") as remote_file:
                payload = remote_file.read()

        if isinstance(payload, bytes):
            text = payload.decode("utf-8", errors="replace")
        else:
            text = payload
        lines = text.splitlines()
        selected = lines[start_line - 1 : end_line]
        return "\n".join(f"{idx}: {line}" for idx, line in enumerate(selected, start=start_line))

    def _walk(
        self,
        sftp,
        current_path: str,
        relative_path: str,
        results: List[Dict[str, object]],
        keyword_lower: str,
        limit: int,
    ) -> None:
        """递归遍历远程文档目录。"""

        if len(results) >= limit:
            return

        for entry in sftp.listdir_attr(current_path):
            remote_path = posixpath.join(current_path, entry.filename)
            rel_path = posixpath.join(relative_path, entry.filename) if relative_path else entry.filename
            mode = entry.st_mode
            if S_ISDIR(mode):
                self._walk(sftp, remote_path, rel_path, results, keyword_lower, limit)
                if len(results) >= limit:
                    return
                continue
            if not S_ISREG(mode):
                continue
            if not any(fnmatch.fnmatch(entry.filename, pattern) for pattern in self.config.patterns):
                continue
            if keyword_lower and keyword_lower not in rel_path.lower():
                continue
            results.append(
                {
                    "path": rel_path,
                    "size": entry.st_size,
                }
            )
            if len(results) >= limit:
                return

    def _open_sftp(self):
        """打开一个临时 SFTP 会话上下文。"""

        if not self.config.enabled:
            raise RuntimeError("knowledge base is disabled")
        if paramiko is None:
            raise RuntimeError("paramiko is not installed")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.config.host,
            port=self.config.port,
            username=self.config.username,
            password=self.config.password,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
            look_for_keys=False,
            allow_agent=False,
        )

        class _SFTPContext:
            def __init__(self, ssh_client):
                self.ssh_client = ssh_client
                self.sftp = None

            def __enter__(self):
                self.sftp = self.ssh_client.open_sftp()
                return self.sftp

            def __exit__(self, exc_type, exc, tb):
                if self.sftp is not None:
                    self.sftp.close()
                self.ssh_client.close()

        return _SFTPContext(client)
