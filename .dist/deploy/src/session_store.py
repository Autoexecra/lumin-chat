import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

from src.models import SessionState


class SessionStore:
    def __init__(self, root_dir: str):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def create(self, model_level: int, approval_policy: str, cwd: str, system_prompt: str) -> SessionState:
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
        session_path = self.get_path(session.session_id)
        with session_path.open("w", encoding="utf-8") as handle:
            json.dump(asdict(session), handle, ensure_ascii=False, indent=2)
        return session_path

    def load(self, session_id_or_path: str) -> SessionState:
        candidate = Path(session_id_or_path)
        path = candidate if candidate.exists() else self.get_path(session_id_or_path)
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return SessionState(**payload)

    def latest(self) -> Optional[SessionState]:
        sessions = sorted(self.root_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not sessions:
            return None
        return self.load(str(sessions[0]))

    def get_path(self, session_id: str) -> Path:
        return self.root_dir / f"{session_id}.json"
