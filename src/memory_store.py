# Copyright (c) 2026 Autoexecra
# Licensed under the Apache License, Version 2.0.
# See LICENSE in the project root for license terms.

"""会话级长期记忆存储与召回。"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_./:-]{2,}|[\u4e00-\u9fff]{2,}")
NOTE_KEYWORDS = (
    "记住",
    "偏好",
    "习惯",
    "默认",
    "总是",
    "不要",
    "必须",
    "优先",
    "项目",
    "开发板",
    "测试板",
    "构建服务器",
    "部署",
    "文档库",
    "rpm",
    "中文",
    "黑名单",
    "白名单",
    "模型",
    "端口",
    "主机",
    "密码",
    "用户名",
    "路径",
    "tl3588",
    "lumin-chat",
)


@dataclass
class MemoryItem:
    """单条长期记忆。"""

    memory_id: int
    title: str
    summary: str
    created_at: str
    score: float = 0.0


class MemoryStore:
    """基于 SQLite 的会话长期记忆存储。"""

    def __init__(self, root_dir: str):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root_dir / "memory.db"
        self.fts_enabled = False
        self._initialize()

    def ensure_session(self, session_id: str, created_at: str | None = None) -> None:
        """确保会话对应的长期记忆元数据已建立。"""

        now = created_at or self._utcnow()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions(session_id, created_at, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (session_id, now, now),
            )
            connection.commit()

    def record_turn(self, session_id: str, user_input: str, assistant_output: str) -> None:
        """把单轮对话沉淀为长期记忆。"""

        if not (user_input or assistant_output):
            return

        self.ensure_session(session_id)
        created_at = self._utcnow()
        title = self._build_title(user_input)
        summary = self._build_summary(user_input, assistant_output)
        content = self._build_content(user_input, assistant_output)
        keywords = " ".join(self._tokenize(user_input + "\n" + assistant_output)[:24])
        importance = self._estimate_importance(user_input)

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_items(session_id, created_at, title, summary, content, keywords, importance)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, created_at, title, summary, content, keywords, importance),
            )
            for note in self._extract_notes(user_input):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO session_notes(session_id, note, created_at)
                    VALUES(?, ?, ?)
                    """,
                    (session_id, note, created_at),
                )
            connection.execute(
                "UPDATE sessions SET updated_at=? WHERE session_id=?",
                (created_at, session_id),
            )
            connection.commit()

    def query(self, session_id: str, query_text: str, limit: int = 5) -> List[MemoryItem]:
        """查询与当前问题最相关的会话长期记忆。"""

        self.ensure_session(session_id)
        limit = max(1, min(int(limit), 10))
        tokens = self._tokenize(query_text)
        if self.fts_enabled and tokens:
            return self._query_via_fts(session_id, tokens, limit)
        return self._query_via_overlap(session_id, tokens, limit)

    def get_notes(self, session_id: str, limit: int = 8) -> List[str]:
        """读取会话沉淀下来的稳定偏好与事实。"""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT note
                FROM session_notes
                WHERE session_id=?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (session_id, max(1, min(int(limit), 20))),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def relevant_notes(self, session_id: str, query_text: str, limit: int = 6) -> List[str]:
        """只返回与当前查询相关的稳定偏好与事实。"""

        query_tokens = set(self._tokenize(query_text))
        ranked: List[tuple[float, str]] = []
        for note in self.get_notes(session_id, limit=50):
            note_tokens = set(self._tokenize(note))
            overlap = len(query_tokens.intersection(note_tokens))
            score = float(overlap)
            if any(keyword in note for keyword in ("中文", "黑名单", "白名单", "审批模式")):
                score += 0.5
            if score <= 0:
                continue
            ranked.append((score, note))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in ranked[: max(1, min(int(limit), 20))]]

    def build_context(self, session_id: str, query_text: str, limit: int = 5, max_chars: int = 1600) -> str:
        """把长期记忆整理成可注入模型的上下文。"""

        notes = self.relevant_notes(session_id, query_text, limit=6)
        memories = self.query(session_id, query_text, limit=limit)
        if not notes and not memories:
            return ""

        lines = ["以下是当前会话的长期记忆，仅在与当前问题相关时参考："]
        if notes:
            lines.append("[稳定偏好/事实]")
            for note in notes:
                lines.append(f"- {note}")
        if memories:
            lines.append("[相关历史片段]")
            for index, item in enumerate(memories, start=1):
                lines.append(f"{index}. {item.title}")
                lines.append(f"   摘要: {item.summary}")
        text = "\n".join(lines)
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n...<长期记忆已截断>..."

    def describe(self, session_id: str) -> Dict[str, object]:
        """返回会话长期记忆概况。"""

        with self._connect() as connection:
            memory_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM memory_items WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
            )
            note_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM session_notes WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
            )
        return {
            "session_id": session_id,
            "memory_count": memory_count,
            "note_count": note_count,
            "fts_enabled": self.fts_enabled,
            "db_path": str(self.db_path),
        }

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions(
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_items(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    content TEXT NOT NULL,
                    keywords TEXT NOT NULL,
                    importance INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_notes(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, note),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
                """
            )
            try:
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts USING fts5(
                        title,
                        summary,
                        content,
                        keywords,
                        session_id UNINDEXED,
                        content='memory_items',
                        content_rowid='id'
                    )
                    """
                )
                connection.executescript(
                    """
                    CREATE TRIGGER IF NOT EXISTS memory_items_ai AFTER INSERT ON memory_items BEGIN
                        INSERT INTO memory_items_fts(rowid, title, summary, content, keywords, session_id)
                        VALUES (new.id, new.title, new.summary, new.content, new.keywords, new.session_id);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memory_items_ad AFTER DELETE ON memory_items BEGIN
                        INSERT INTO memory_items_fts(memory_items_fts, rowid, title, summary, content, keywords, session_id)
                        VALUES('delete', old.id, old.title, old.summary, old.content, old.keywords, old.session_id);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memory_items_au AFTER UPDATE ON memory_items BEGIN
                        INSERT INTO memory_items_fts(memory_items_fts, rowid, title, summary, content, keywords, session_id)
                        VALUES('delete', old.id, old.title, old.summary, old.content, old.keywords, old.session_id);
                        INSERT INTO memory_items_fts(rowid, title, summary, content, keywords, session_id)
                        VALUES (new.id, new.title, new.summary, new.content, new.keywords, new.session_id);
                    END;
                    """
                )
                self.fts_enabled = True
            except sqlite3.OperationalError:
                self.fts_enabled = False
            connection.commit()

    def _query_via_fts(self, session_id: str, tokens: List[str], limit: int) -> List[MemoryItem]:
        matcher = " OR ".join(self._escape_fts_token(token) for token in tokens[:8])
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT mi.id, mi.title, mi.summary, mi.created_at, bm25(memory_items_fts, 1.0, 2.0, 0.5, 0.2) AS score
                FROM memory_items_fts
                JOIN memory_items mi ON mi.id = memory_items_fts.rowid
                WHERE memory_items_fts MATCH ? AND mi.session_id=?
                ORDER BY score, mi.importance DESC, mi.created_at DESC
                LIMIT ?
                """,
                (matcher, session_id, limit),
            ).fetchall()
        return [
            MemoryItem(memory_id=int(row[0]), title=str(row[1]), summary=str(row[2]), created_at=str(row[3]), score=float(row[4]))
            for row in rows
        ]

    def _query_via_overlap(self, session_id: str, tokens: List[str], limit: int) -> List[MemoryItem]:
        token_set = set(tokens)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, summary, created_at, content, keywords, importance
                FROM memory_items
                WHERE session_id=?
                ORDER BY importance DESC, created_at DESC
                """,
                (session_id,),
            ).fetchall()
        scored: List[MemoryItem] = []
        for row in rows:
            joined = f"{row[1]}\n{row[2]}\n{row[4]}\n{row[5]}"
            score = float(len(token_set.intersection(self._tokenize(joined)))) + float(row[6]) * 0.3
            if token_set and score <= 0:
                continue
            scored.append(
                MemoryItem(
                    memory_id=int(row[0]),
                    title=str(row[1]),
                    summary=str(row[2]),
                    created_at=str(row[3]),
                    score=score,
                )
            )
        scored.sort(key=lambda item: (item.score, item.created_at), reverse=True)
        return scored[:limit]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _utcnow() -> str:
        return datetime.utcnow().isoformat() + "Z"

    @staticmethod
    def _build_title(user_input: str) -> str:
        compact = re.sub(r"\s+", " ", user_input.strip())
        return compact[:48] if compact else "未命名会话记忆"

    @staticmethod
    def _build_summary(user_input: str, assistant_output: str) -> str:
        user_text = re.sub(r"\s+", " ", user_input.strip())
        assistant_text = re.sub(r"\s+", " ", assistant_output.strip())
        if assistant_text:
            return f"用户: {user_text[:80]}；助手: {assistant_text[:120]}"
        return f"用户: {user_text[:160]}"

    @staticmethod
    def _build_content(user_input: str, assistant_output: str) -> str:
        return json.dumps(
            {
                "user": user_input.strip(),
                "assistant": assistant_output.strip(),
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _estimate_importance(user_input: str) -> int:
        score = 1
        lowered = user_input.lower()
        for keyword in ("记住", "默认", "必须", "优先", "不要", "tl3588", "配置", "deploy", "rpm"):
            if keyword in lowered or keyword in user_input:
                score += 1
        return min(score, 5)

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        seen = set()
        tokens: List[str] = []
        for match in TOKEN_PATTERN.findall(text or ""):
            token = match.strip().lower()
            if len(token) < 2 or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
        return tokens

    @staticmethod
    def _extract_notes(user_input: str) -> List[str]:
        candidates = re.split(r"[。！？!?.\n；;]+", user_input)
        notes: List[str] = []
        seen = set()
        for item in candidates:
            sentence = re.sub(r"\s+", " ", item.strip())
            if not sentence or len(sentence) < 6 or len(sentence) > 160:
                continue
            if not any(keyword in sentence for keyword in NOTE_KEYWORDS):
                continue
            normalized = sentence.rstrip("，, ")
            if normalized in seen:
                continue
            seen.add(normalized)
            notes.append(normalized)
            if len(notes) >= 8:
                break
        return notes

    @staticmethod
    def _escape_fts_token(token: str) -> str:
        safe = token.replace('"', ' ')
        return f'"{safe}"'
