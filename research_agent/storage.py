import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = threading.RLock()
        self.db = sqlite3.connect(directory / "sessions.sqlite", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (user_id INTEGER PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.db.commit()

    def authorized(self, user_id: int, allowlist: set[int], claim: bool = False) -> bool:
        if allowlist:
            return user_id in allowlist
        with self.lock:
            row = self.db.execute("SELECT value FROM metadata WHERE key='owner'").fetchone()
            if row:
                return str(user_id) == row[0]
            if claim:
                self.db.execute("INSERT INTO metadata VALUES ('owner', ?)", (str(user_id),))
                self.db.commit()
                return True
        return False

    def get(self, user_id: int) -> dict:
        with self.lock:
            row = self.db.execute("SELECT state FROM sessions WHERE user_id=?", (user_id,)).fetchone()
        return json.loads(row[0]) if row else {"stage": "area", "answers": {}, "followups": []}

    def save(self, user_id: int, state: dict):
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?, ?)",
                (user_id, json.dumps(state, ensure_ascii=False)),
            )
            self.db.commit()

    def audit(self, run_id: str, event: str, **details):
        directory = self.directory / "runs" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        payload = {"time": datetime.now(UTC).isoformat(), "event": event, **details}
        with self.lock, (directory / "audit.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def recover(self):
        with self.lock:
            rows = self.db.execute("SELECT user_id, state FROM sessions").fetchall()
        for user_id, raw in rows:
            state = json.loads(raw)
            if state.get("stage") == "running":
                state.update(
                    stage="failed", error="Процесс был перезапущен; /retry продолжит сохраненный граф."
                )
                self.save(user_id, state)
