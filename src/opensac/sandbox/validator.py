from __future__ import annotations

import ast


class UnsafeCodeError(ValueError):
    pass


BLOCKED_MODULES = {
    "ctypes",
    "httpx",
    "multiprocessing",
    "requests",
    "resource",
    "shutil",
    "signal",
    "socket",
    "subprocess",
}

BLOCKED_CALLS = {"__import__", "breakpoint", "compile", "eval", "exec", "help", "input"}


def validate_code(code: str) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise UnsafeCodeError(f"Generated code is invalid Python: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = {alias.name.split(".", 1)[0] for alias in node.names}
            blocked = modules & BLOCKED_MODULES
            if blocked:
                raise UnsafeCodeError(f"Blocked imports: {', '.join(sorted(blocked))}")
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".", 1)[0]
            if module in BLOCKED_MODULES:
                raise UnsafeCodeError(f"Blocked import: {module}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in BLOCKED_CALLS:
                raise UnsafeCodeError(f"Blocked call: {node.func.id}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise UnsafeCodeError("Dunder attribute access is not allowed")
