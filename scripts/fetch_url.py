from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from opportunity_radar.fetching import FetchError, HttpPageFetcher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch and inspect one real HTML URL.")
    parser.add_argument("url")
    parser.add_argument("--preview-chars", type=int, default=800)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--maximum-bytes", type=int, default=2_000_000)
    args = parser.parse_args(argv)
    if args.preview_chars < 0:
        parser.error("--preview-chars must be non-negative")

    try:
        page = HttpPageFetcher(
            timeout_seconds=args.timeout,
            maximum_bytes=args.maximum_bytes,
        ).fetch(args.url)
    except FetchError as exc:
        print(f"Fetch failed [{exc.kind.value}]: {exc}", file=sys.stderr)
        return 1

    print(f"Requested URL: {page.requested_url}")
    print(f"Resolved URL: {page.final_url}")
    print(f"HTTP status: {page.status_code}")
    print(f"Content-Type: {page.content_type}")
    print(f"Page title: {page.page_title or 'Unknown'}")
    print(f"Response bytes: {page.byte_count}")
    print(f"Cleaned-text characters: {len(page.cleaned_text)}")
    print("Preview:")
    print(page.cleaned_text[: args.preview_chars])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
