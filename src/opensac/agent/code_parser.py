from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

PYTHON_BLOCK = re.compile(r"```python\s*(.*?)```", re.DOTALL | re.IGNORECASE)
FINAL_BLOCK = re.compile(r"<final>\s*(.*?)\s*</final>", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class AgentAction:
    code: str | None = None
    final: dict[str, Any] | None = None


def parse_action(text: str) -> AgentAction:
    final_match = FINAL_BLOCK.search(text)
    if final_match:
        try:
            payload = json.loads(final_match.group(1))
        except json.JSONDecodeError as exc:
            raise ValueError("The <final> block must contain valid JSON") from exc
        return AgentAction(final=payload)
    code_match = PYTHON_BLOCK.search(text)
    if code_match:
        return AgentAction(code=code_match.group(1).strip())
    raise ValueError("Model response contained neither Python code nor a <final> block")
