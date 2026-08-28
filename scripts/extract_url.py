from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from opportunity_radar.extraction import (
    ExtractionError,
    OpenAIFactualExtractionProvider,
    SemanticOpportunityExtractor,
)
from opportunity_radar.fetching import FetchError, HttpPageFetcher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch one URL and extract factual opportunity JSON.")
    parser.add_argument("url")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--maximum-bytes", type=int, default=2_000_000)
    parser.add_argument("--semantic-input-limit", type=int, default=50_000)
    args = parser.parse_args(argv)
    try:
        page = HttpPageFetcher(
            timeout_seconds=args.timeout,
            maximum_bytes=args.maximum_bytes,
        ).fetch(args.url)
        provider = OpenAIFactualExtractionProvider.from_environment()
        opportunity = SemanticOpportunityExtractor(
            provider,
            semantic_input_limit=args.semantic_input_limit,
        ).extract(page)
    except FetchError as exc:
        print(f"Fetch failed [{exc.kind.value}]: {exc}", file=sys.stderr)
        return 1
    except ExtractionError as exc:
        print(f"Extraction failed [{exc.kind.value}]: {exc}", file=sys.stderr)
        return 1
    print(opportunity.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
