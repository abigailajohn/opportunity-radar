from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path: sys.path.insert(0, str(SRC))

from opportunity_radar.composition import build_provider_bundle, provider_mode_from_environment
from opportunity_radar.discovery_models import load_source_configuration
from opportunity_radar.milestone2 import run_discovery_pipeline, write_milestone2_outputs
from opportunity_radar.overrides import OpportunityOverrideApplier, load_overrides
from opportunity_radar.persistence import PostgresOpportunityStore
from opportunity_radar.profile import load_profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover and evaluate opportunities from configured trusted sources.")
    parser.add_argument("source_config", type=Path)
    parser.add_argument("--profile", type=Path, default=ROOT / "config" / "profile.yaml")
    parser.add_argument("--overrides", type=Path, default=ROOT / "config" / "opportunity_overrides.yaml")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "discovery")
    parser.add_argument("--mode", choices=("deterministic", "openai"), default=None)
    args = parser.parse_args(argv)
    try:
        configuration = load_source_configuration(args.source_config); profile = load_profile(args.profile)
        mode = provider_mode_from_environment(args.mode); providers = build_provider_bundle(mode)
        overrides = OpportunityOverrideApplier(load_overrides(args.overrides), source_file=str(args.overrides))
        with PostgresOpportunityStore.from_environment() as store:
            result = run_discovery_pipeline(configuration, profile, providers.fetcher, providers.extractor, providers.assessor, store, as_of=date.today(), opportunity_transform=overrides.apply)
        write_milestone2_outputs(result, args.output)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    counts = result.counts
    print(f"Sources fetched: {counts['sources_fetched']}")
    print(f"Candidate links found: {counts['candidate_links_found']}")
    print("Candidate classifications:")
    for key, value in sorted(counts.items()):
        if key.startswith("classification_"): print(f"  {key.removeprefix('classification_')}: {value}")
    print(f"Specific opportunities evaluated: {counts['specific_opportunities_evaluated']}")
    print(f"New opportunities: {counts['new_opportunities']}")
    print(f"Changed opportunities: {counts['changed_opportunities']}")
    print(f"Failures: {counts['failures']}")
    print(f"Provider mode: {mode.value}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
