"""会话状态的本地持久化。"""

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

from src.models import SessionState


class SessionStore:
    """把会话保存到磁盘并支持恢复。"""

    def __init__(self, root_dir: str):
        """初始化会话目录。"""

        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def create(self, model_level: int, approval_policy: str, cwd: str, system_prompt: str) -> SessionState:
        """创建一个新的会话状态并落盘。"""

        session = SessionState(
            session_id=uuid4().hex,
            created_at=datetime.utcnow().isoformat() + "Z",
            model_level=model_level,
            approval_policy=approval_policy,
            cwd=cwd,
            messages=[{"role": "system", "content": system_prompt}],
        )
        self.save(session)
        return session

    def save(self, session: SessionState) -> Path:
        """保存会话到磁盘。"""

        session_path = self.get_path(session.session_id)
        with session_path.open("w", encoding="utf-8") as handle:
            json.dump(asdict(session), handle, ensure_ascii=False, indent=2)
        return session_path

    def load(self, session_id_or_path: str) -> SessionState:
        """按 ID 或路径加载会话。"""

        candidate = Path(session_id_or_path)
        path = candidate if candidate.exists() else self.get_path(session_id_or_path)
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return SessionState(**payload)

    def latest(self) -> Optional[SessionState]:
        """获取最近一次保存的会话。"""

        sessions = sorted(self.root_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not sessions:
            return None
        return self.load(str(sessions[0]))

    def list_sessions(self, limit: int = 20) -> List[Dict[str, str]]:
        """列出最近的会话摘要，供切换与检查使用。"""

        session_files = sorted(self.root_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        results: List[Dict[str, str]] = []
        for path in session_files[: max(1, min(int(limit), 100))]:
            try:
                session = self.load(str(path))
            except Exception:
                continue
            preview = ""
            for message in session.messages:
                if message.get("role") == "user":
                    preview = str(message.get("content", "")).strip().replace("\n", " ")[:80]
                    break
            results.append(
                {
                    "session_id": session.session_id,
                    "created_at": session.created_at,
                    "cwd": session.cwd,
                    "path": str(path),
                    "preview": preview or "<empty>",
                }
            )
        return results

    def get_path(self, session_id: str) -> Path:
        """根据会话 ID 生成保存路径。"""

        return self.root_dir / f"{session_id}.json"
