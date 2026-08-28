from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from opportunity_radar.composition import build_provider_bundle, provider_mode_from_environment
from opportunity_radar.milestone3 import run_search_pipeline, write_milestone3_outputs
from opportunity_radar.overrides import OpportunityOverrideApplier, load_overrides
from opportunity_radar.persistence import PostgresOpportunityStore
from opportunity_radar.profile import load_profile
from opportunity_radar.search import JsonReplaySearchProvider, TavilySearchProvider, UnavailableSearchProvider
from opportunity_radar.search_models import SearchConfiguration


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate profile-based searches and evaluate open-web opportunity results.")
    parser.add_argument("--profile", type=Path, default=ROOT / "config" / "profile.yaml")
    parser.add_argument("--overrides", type=Path, default=ROOT / "config" / "opportunity_overrides.yaml")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "search")
    parser.add_argument("--mode", choices=("deterministic", "openai"), default=None, help="Factual extraction mode; deterministic is the default.")
    defaults = SearchConfiguration()
    parser.add_argument("--search-provider", choices=("tavily", "unavailable", "replay"), default=os.getenv("OPPORTUNITY_RADAR_SEARCH_PROVIDER", "tavily"))
    parser.add_argument("--search-results", type=Path, help="JSON results file required by the replay provider.")
    parser.add_argument("--max-queries", type=int, default=defaults.max_queries_per_run)
    parser.add_argument("--max-results-per-query", type=int, default=defaults.max_results_per_query)
    parser.add_argument("--candidate-cap", type=int, default=defaults.global_candidate_cap)
    parser.add_argument("--fetch-cap", type=int, default=defaults.fetch_cap)
    args = parser.parse_args(argv)
    try:
        profile = load_profile(args.profile)
        mode = provider_mode_from_environment(args.mode)
        providers = build_provider_bundle(mode)
        if args.search_provider == "tavily":
            search_provider = TavilySearchProvider.from_environment()
        elif args.search_provider == "replay":
            if args.search_results is None:
                raise ValueError("--search-results is required with --search-provider replay")
            search_provider = JsonReplaySearchProvider.from_file(args.search_results)
        else:
            search_provider = UnavailableSearchProvider()
        configuration = SearchConfiguration(
            max_queries_per_run=args.max_queries,
            max_results_per_query=args.max_results_per_query,
            global_candidate_cap=args.candidate_cap,
            fetch_cap=args.fetch_cap,
        )
        overrides = OpportunityOverrideApplier(load_overrides(args.overrides), source_file=str(args.overrides))
        with PostgresOpportunityStore.from_environment() as store:
            result = run_search_pipeline(
                profile, search_provider, providers.fetcher, providers.extractor, providers.assessor,
                store, configuration=configuration, as_of=date.today(), opportunity_transform=overrides.apply,
            )
        write_milestone3_outputs(result, args.output)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    counts = result.counts
    print(f"Queries generated: {counts['queries']}")
    print(f"Candidates found: {counts['candidates']}")
    print(f"Specific opportunities evaluated: {counts['specific_opportunities_evaluated']}")
    print(f"New opportunities: {counts['new_opportunities']}")
    print(f"Unchanged opportunities: {counts['unchanged_opportunities']}")
    print(f"Changed opportunities: {counts['changed_opportunities']}")
    print(f"Failures: {counts['failures']}")
    print(f"Extraction mode: {mode.value}")
    print(f"Search provider: {search_provider.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
