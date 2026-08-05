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

# Dunders that cannot be pivoted on. Both evaluate to a plain string, so they
# start no chain toward `__class__` -> `__subclasses__` -> arbitrary objects,
# which is what the blanket ban exists to interrupt.
#
# They are worth exempting because `type(exc).__name__` is how Python code
# reports an error, and generated pipelines wrap their retrieval in try/except
# constantly. Refusing the whole program over an error message costs the
# control model a turn and teaches it nothing: the rejection names a rule, not
# the harmless line that tripped it.
#
# The ban is defence in depth, not the security boundary. It is bypassable by
# construction -- `getattr(obj, "__cla" + "ss__")` never reaches this check --
# and the actual containment is the container: no network, read-only root, all
# capabilities dropped, and a broker socket that only answers to this session's
# token. Widening it by two inert strings does not move that boundary.
ALLOWED_DUNDER_ATTRIBUTES = {"__name__", "__doc__"}


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
            if node.attr in ALLOWED_DUNDER_ATTRIBUTES:
                continue
            raise UnsafeCodeError(
                f"Dunder attribute access is not allowed: {node.attr}. "
                f"Only {', '.join(sorted(ALLOWED_DUNDER_ATTRIBUTES))} are permitted."
            )
