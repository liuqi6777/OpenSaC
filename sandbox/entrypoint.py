from __future__ import annotations

import os
import runpy
import sys
import time
from contextlib import suppress
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: entrypoint.py PROGRAM")
    program = Path(sys.argv[1]).resolve()
    workspace = Path("/workspace").resolve()
    if not program.is_relative_to(workspace):
        raise SystemExit("program must be inside /workspace")
    ready_path = os.environ.get("OPENSAC_READY_PATH", "").strip()
    if ready_path:
        # Timing instrumentation must never decide whether user code runs.
        with suppress(OSError):
            Path(ready_path).write_text(str(time.time_ns()), encoding="utf-8")
    runpy.run_path(str(program), run_name="__main__")


if __name__ == "__main__":
    main()
