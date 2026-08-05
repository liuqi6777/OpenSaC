from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: entrypoint.py PROGRAM")
    program = Path(sys.argv[1]).resolve()
    workspace = Path("/workspace").resolve()
    if not program.is_relative_to(workspace):
        raise SystemExit("program must be inside /workspace")
    runpy.run_path(str(program), run_name="__main__")


if __name__ == "__main__":
    main()
