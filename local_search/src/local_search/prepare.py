from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_REPO_ID = "liuqi6777/Browsecomp-Plus-Indexes"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "indexes/browsecomp-plus"
REQUIRED_FILES = (
    "index.faiss",
    "index.faiss.metadata.json",
    "index_ids.json",
    "corpus.jsonl",
)


def _download_file(
    *,
    repo_id: str,
    filename: str,
    revision: str,
    destination: Path,
) -> None:
    url = (
        "https://huggingface.co/datasets/"
        f"{quote(repo_id, safe='/')}/resolve/{quote(revision, safe='')}/"
        f"{quote(filename, safe='/')}?download=true"
    )
    headers = {"User-Agent": "opensac-local-search/0.1"}
    token = os.getenv("HF_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    temporary = destination.with_name(f".{destination.name}.part")
    request = Request(url, headers=headers)
    try:
        with urlopen(request) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def download_index(
    *,
    repo_id: str,
    output_dir: Path,
    revision: str = "main",
    force_download: bool = False,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    for filename in REQUIRED_FILES:
        path = output_dir / filename
        if path.is_file() and not force_download:
            downloaded.append(path)
            continue
        _download_file(
            repo_id=repo_id,
            filename=filename,
            revision=revision,
            destination=path,
        )
        downloaded.append(path)
    return downloaded


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download a ready-to-load BrowseComp-Plus dense index."
    )
    parser.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
    )
    parser.add_argument(
        "--revision",
        default="main",
        help="Optional branch, tag, or commit hash to pin reproducibly.",
    )
    parser.add_argument("--force-download", action="store_true")
    args = parser.parse_args()

    paths = download_index(
        repo_id=args.repo_id,
        output_dir=args.output_dir,
        revision=args.revision,
        force_download=args.force_download,
    )
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
