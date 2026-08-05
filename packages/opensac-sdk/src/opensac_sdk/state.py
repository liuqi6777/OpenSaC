from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class StateResource:
    def __init__(self, workspace: str) -> None:
        self._workspace = Path(workspace).resolve()

    def _path(self, relative_path: str) -> Path:
        path = (self._workspace / relative_path).resolve()
        if not path.is_relative_to(self._workspace):
            raise ValueError("State path must remain inside the session workspace")
        return path

    def write_jsonl(self, relative_path: str, rows: list[Any]) -> None:
        path = self._path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=True, default=str) + "\n")

    def read_jsonl(self, relative_path: str) -> list[Any]:
        path = self._path(relative_path)
        with path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def write_json(self, relative_path: str, value: Any) -> None:
        path = self._path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(value, ensure_ascii=True, indent=2, default=str)
        path.write_text(payload, encoding="utf-8")

    def read_json(self, relative_path: str) -> Any:
        return json.loads(self._path(relative_path).read_text(encoding="utf-8"))

    @classmethod
    def from_environment(cls) -> StateResource:
        return cls(os.environ.get("OPENSAC_WORKSPACE", "/workspace"))
