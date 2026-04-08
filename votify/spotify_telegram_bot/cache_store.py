from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class CachedFile:
    method: str  # audio | video | document
    file_id: str
    file_unique_id: str
    file_name: str
    created_at: str
    title: str = ""
    performer: str = ""
    duration: int = 0


class TelegramFileCacheStore:
    def __init__(self, cache_path: str) -> None:
        self.cache_path = Path(cache_path)
        self._items: dict[str, list[CachedFile]] = {}
        self._load()

    def _load(self) -> None:
        if not self.cache_path.exists():
            self._items = {}
            return

        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            self._items = {}
            return

        parsed: dict[str, list[CachedFile]] = {}
        if isinstance(data, dict):
            for key, arr in data.items():
                if not isinstance(key, str) or not isinstance(arr, list):
                    continue
                records: list[CachedFile] = []
                for row in arr:
                    if not isinstance(row, dict):
                        continue
                    try:
                        try:
                            duration = int(row.get("duration") or 0)
                        except Exception:
                            duration = 0
                        records.append(
                            CachedFile(
                                method=str(row.get("method", "")).strip(),
                                file_id=str(row.get("file_id", "")).strip(),
                                file_unique_id=str(row.get("file_unique_id", "")).strip(),
                                file_name=str(row.get("file_name", "")).strip(),
                                created_at=str(row.get("created_at", "")).strip(),
                                title=str(row.get("title", "")).strip(),
                                performer=str(row.get("performer", "")).strip(),
                                duration=duration,
                            )
                        )
                    except Exception:
                        continue
                if records:
                    parsed[key] = records
        self._items = parsed

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            key: [asdict(entry) for entry in entries]
            for key, entries in self._items.items()
        }
        tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.cache_path)

    def get(self, key: str) -> list[CachedFile]:
        return list(self._items.get(key, []))

    def put(self, key: str, entries: list[CachedFile]) -> None:
        if not key:
            return
        valid = [i for i in entries if i.file_id and i.method]
        if not valid:
            return
        self._items[key] = valid
        self._save()

    def delete(self, key: str) -> None:
        if key in self._items:
            del self._items[key]
            self._save()

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
