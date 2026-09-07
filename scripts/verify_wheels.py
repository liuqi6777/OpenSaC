"""Verify the local SDK wheel in an isolated environment with an external provider.

Run from the repository root after building the distribution:
    uv run python scripts/verify_wheels.py

Uses temporary Python 3.12 environments, no provider credentials, no paid services.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def command(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Command failed: {args[0]}\n{result.stdout}\n{result.stderr}")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    wheel = next((root / "dist").glob("opensac-*.whl"))

    class FetchService(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/fetch":
                assert payload["url"] == "http://localhost:9000/paper.html"
                result = {
                    "data": {
                        "url": payload["url"],
                        "text": "Document from independent fetch service",
                    }
                }
            elif self.path == "/rerank":
                result = {
                    "results": [
                        {"index": i, "relevance_score": float(i)}
                        for i in reversed(range(len(payload["documents"])))
                    ][: payload["top_n"]]
                }
            elif self.path == "/chat/completions":
                result = {
                    "model": "probe-model",
                    "choices": [{"finish_reason": "stop", "message": {"content": '{"count": 3}'}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 5, "total_tokens": 7},
                }
            else:
                raise AssertionError("Unexpected provider path")
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FetchService)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="opensac-wheel-check-") as temporary:
            directory = Path(temporary)
            sdk_env = directory / "sdk-env"
            command(["uv", "venv", "--python", "3.12", str(sdk_env)])
            sdk_python = sdk_env / "bin/python"
            command(["uv", "pip", "install", "--python", str(sdk_python), str(wheel)])

            # Build and install a third-party distribution, exercising real metadata discovery.
            package = directory / "search-provider"
            package.mkdir()
            (package / "pyproject.toml").write_text("""[project]
name = "opensac-wheel-probe"
version = "0.0.1"
[project.entry-points."opensac.search"]
wheel-probe = "probe:Search"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[tool.hatch.build.targets.wheel]
packages = ["probe.py"]
""")
            (package / "probe.py").write_text("""from opensac.contracts import SearchHit

class Search:
    def __init__(self, config, context):
        pass

    async def search(self, query, limit):
        return [SearchHit(
            url="http://localhost:9000/paper.html", title=query, snippet="Found"
        )]

    async def aclose(self):
        pass
""")
            command(["uv", "build", "--wheel"], cwd=package)
            provider_wheel = next((package / "dist").glob("*.whl"))
            command(["uv", "pip", "install", "--python", str(sdk_python), str(provider_wheel)])
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("OPENSAC_") and key not in {"PYTHONPATH", "VIRTUAL_ENV"}
            }
            env.update(
                {
                    "OPENSAC_SEARCH_PROVIDER": "wheel-probe",
                    "OPENSAC_FETCH_PROVIDER": "http",
                    "OPENSAC_FETCH_BASE_URL": f"http://127.0.0.1:{upstream.server_port}",
                    "OPENSAC_RERANK_BASE_URL": f"http://127.0.0.1:{upstream.server_port}",
                    "OPENSAC_RERANK_MODEL": "probe-ranker",
                    "OPENSAC_LLM_BASE_URL": f"http://127.0.0.1:{upstream.server_port}",
                    "OPENSAC_LLM_MODEL": "probe-model",
                    "NO_PROXY": "127.0.0.1,localhost",
                }
            )
            direct_code = """import importlib.util
import json
from pathlib import Path
from opensac import sdk, fuse, dedup

assert importlib.util.find_spec("fastapi") is None
assert importlib.util.find_spec("uvicorn") is None
assert importlib.util.find_spec("opensac.server") is None
hits = sdk.search("direct wheel integration")
assert dedup(hits + hits) == hits
assert fuse([hits, hits]) == hits
document = sdk.content.fetch(hits[0].url)
assert document.text == "Document from independent fetch service"
Path("direct-result.json").write_text(document.model_dump_json())
assert sdk.content.fetch_many([hits[0].url])[0].data.text == document.text
assert sdk.rerank("query", ["a", "b"], top_n=1) == ["b"]
assert sdk.llm.complete("hello").usage.total_tokens == 7
schema = {"type": "object", "properties": {"count": {"type": "integer"}},
          "required": ["count"], "additionalProperties": False}
assert sdk.llm.extract("three", schema).data == {"count": 3}
assert sdk.llm.extract_many(["three"], schema)[0].data.data == {"count": 3}
assert sdk.llm.complete_many(["hello"])[0].data.model == "probe-model"
sdk.close()
assert importlib.util.find_spec("opensac.server") is None
"""
            command([str(sdk_python), "-c", direct_code], cwd=directory, env=env)
            command([str(sdk_python), str(root / "examples/compose.py")], cwd=directory, env=env)
        print("PASS: Python 3.12 wheel, local SDK, external provider, rerank/LLM, local files")
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
