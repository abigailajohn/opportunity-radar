from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from opportunity_radar.pipeline import InputValidationError, load_url_file, run_pipeline, write_outputs
from opportunity_radar.profile import load_profile
from opportunity_radar.composition import build_provider_bundle, provider_mode_from_environment
from opportunity_radar.overrides import OpportunityOverrideApplier, load_overrides


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate exactly ten opportunity URLs.")
    parser.add_argument("url_file", type=Path)
    parser.add_argument("--profile", type=Path, default=ROOT / "config" / "profile.yaml")
    parser.add_argument("--output", type=Path, default=ROOT / "output")
    parser.add_argument("--overrides", type=Path, default=ROOT / "config" / "opportunity_overrides.yaml")
    parser.add_argument("--mode", choices=("deterministic", "openai"), default=None)
    args = parser.parse_args(argv)
    try:
        urls = load_url_file(args.url_file)
        profile = load_profile(args.profile)
        mode = provider_mode_from_environment(args.mode)
        providers = build_provider_bundle(mode)
        override_applier = OpportunityOverrideApplier(
            load_overrides(args.overrides),
            source_file=str(args.overrides),
        )
    except (InputValidationError, OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))

    result = run_pipeline(
        urls,
        profile,
        providers.fetcher,
        providers.extractor,
        providers.assessor,
        as_of=date.today(),
        opportunity_transform=override_applier.apply,
    )
    write_outputs(result, args.output)
    print(f"Input URLs: {result.input_count}")
    print(f"Fetched: {result.fetched_count}")
    print(f"Extracted: {len(result.opportunities)}")
    print(f"Failed: {len(result.failures)}")
    print(f"Evaluated: {len(result.assessments)}")
    print(f"Digest entries: {len(result.digest.selected_ids)}")
    print(f"Provider mode: {mode.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
