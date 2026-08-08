from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class StateResource:
    """The workspace, which is how a program takes notes across turns.

    Only what a program writes here survives the end of its execution, so this
    is the whole of a rollout's memory apart from what it printed. That makes
    three operations load-bearing rather than convenient: appending (so
    accumulating evidence is not read-everything-then-write-everything),
    asking whether a file is there (so a later turn can tell "the earlier turn
    saved nothing" from "the earlier turn never ran"), and listing (so it can
    find out what it has without guessing names).
    """

    def __init__(self, workspace: str) -> None:
        self._workspace = Path(workspace).resolve()

    def _path(self, relative_path: str) -> Path:
        path = (self._workspace / relative_path).resolve()
        if not path.is_relative_to(self._workspace):
            raise ValueError("State path must remain inside the session workspace")
        return path

    @staticmethod
    def _dump(rows: list[Any]) -> str:
        return "".join(
            json.dumps(row, ensure_ascii=True, default=str) + "\n" for row in rows
        )

    def write_jsonl(self, relative_path: str, rows: list[Any]) -> None:
        path = self._path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._dump(rows), encoding="utf-8")

    def append_jsonl(self, relative_path: str, rows: list[Any]) -> None:
        """Add rows to a file, creating it if absent.

        Without this, extending a record across turns means reading the whole
        file back and rewriting it, which costs the workspace's size on every
        addition and loses everything if the program dies midway.
        """
        path = self._path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(self._dump(rows))

    def exists(self, relative_path: str) -> bool:
        return self._path(relative_path).is_file()

    def list(self, prefix: str = "") -> list[str]:
        """Workspace-relative paths of the files a program has written.

        Runtime internals are hidden: the `.opensac-` files are how the sandbox
        and the harness talk to each other, and a program that read or rewrote
        them would be editing the record of its own execution.
        """
        if not self._workspace.exists():
            return []
        return sorted(
            relative
            for path in self._workspace.rglob("*")
            if path.is_file() and not path.name.startswith(".opensac-")
            for relative in [str(path.relative_to(self._workspace))]
            if relative.startswith(prefix)
        )

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
