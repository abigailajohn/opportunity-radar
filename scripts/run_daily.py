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
from opportunity_radar.discovery_models import load_source_configuration
from opportunity_radar.milestone4 import run_daily, write_daily_outputs
from opportunity_radar.notifications import TelegramNotificationProvider
from opportunity_radar.overrides import OpportunityOverrideApplier, load_overrides
from opportunity_radar.persistence import PostgresOpportunityStore
from opportunity_radar.profile import load_profile
from opportunity_radar.search import TavilySearchProvider
from opportunity_radar.search_models import SearchConfiguration


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one complete Opportunity Radar daily cycle and exit.")
    parser.add_argument("--profile", type=Path, default=ROOT / "config" / "profile.yaml")
    parser.add_argument("--sources", type=Path, default=ROOT / "config" / "sources.yaml")
    parser.add_argument("--overrides", type=Path, default=ROOT / "config" / "opportunity_overrides.yaml")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "daily")
    parser.add_argument("--mode", choices=("deterministic", "openai"), default=None)
    defaults = SearchConfiguration()
    parser.add_argument("--max-queries", type=int, default=defaults.max_queries_per_run)
    parser.add_argument("--max-results-per-query", type=int, default=defaults.max_results_per_query)
    parser.add_argument("--candidate-cap", type=int, default=defaults.global_candidate_cap)
    parser.add_argument("--fetch-cap", type=int, default=defaults.fetch_cap)
    args = parser.parse_args(argv)
    try:
        profile = load_profile(args.profile)
        source_configuration = load_source_configuration(args.sources) if args.sources.exists() else None
        mode = provider_mode_from_environment(args.mode)
        providers = build_provider_bundle(mode)
        search_provider = TavilySearchProvider.from_environment()
        notifier = TelegramNotificationProvider.from_environment()
        overrides = OpportunityOverrideApplier(load_overrides(args.overrides), source_file=str(args.overrides))
        search_configuration = SearchConfiguration(
            max_queries_per_run=args.max_queries,
            max_results_per_query=args.max_results_per_query,
            global_candidate_cap=args.candidate_cap,
            fetch_cap=args.fetch_cap,
        )
        with PostgresOpportunityStore.from_environment() as store:
            result = run_daily(
                profile, search_provider, notifier, providers.fetcher, providers.extractor,
                providers.assessor, store, source_configuration=source_configuration,
                search_configuration=search_configuration, as_of=date.today(),
                opportunity_transform=overrides.apply,
                run_id=os.getenv("GITHUB_RUN_ID") or os.getenv("OPPORTUNITY_RADAR_RUN_ID"),
            )
        write_daily_outputs(result, args.output)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    summary = result.summary
    print("Daily cycle complete")
    print(f"Search queries: {summary.search_queries_executed}")
    print(f"Search results: {summary.search_results_returned}")
    print(f"Candidates fetched: {summary.candidates_checked}")
    print(f"Evaluated: {summary.opportunities_evaluated}")
    print(f"New: {summary.new_opportunities}")
    print(f"Changed: {summary.changed_opportunities}")
    print(f"Notifications selected: {len(result.planned_notifications)}")
    print(f"Telegram deliveries: {sum(item.status.value == 'delivered' and item.chunks_sent > 0 for item in result.deliveries)}")
    print(f"Isolated failures: {summary.isolated_failures}")
    print(f"Notification delivery failures: {sum(item.status.value == 'failed' for item in result.deliveries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
