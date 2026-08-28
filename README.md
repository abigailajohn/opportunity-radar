# Opportunity Radar

Opportunity Radar is a personal opportunity intelligence system designed to help surface, evaluate, and prioritize opportunities that are actually worth paying attention to.

The long-term goal is to continuously discover opportunities across areas such as:

- Internships
- Fellowships
- Cybersecurity and software roles
- AI and emerging technology programs
- Conferences and CFPs
- Travel-funded programs
- Startup grants and founder opportunities
- CTFs, hackathons, and competitions
- Research and open-source programs

Rather than behaving like a generic opportunity board, Opportunity Radar evaluates opportunities against a structured personal profile and ranks them based on factors such as:

- Eligibility
- Relevance
- Opportunity value
- Practical feasibility
- Timing
- Application friction
- Confidence in the available information

## Current Status

The project currently includes Milestone 1 evaluation, Milestone 2 trusted-source discovery, and an experimental Milestone 3 open-web search orchestration layer.

Milestone 1 takes a small set of opportunity URLs and:

1. Fetches and prepares the source pages
2. Extracts structured opportunity facts
3. Evaluates them against a structured profile
4. Deduplicates repeated opportunities
5. Scores and prioritizes them
6. Produces a ranked digest

The current default mode is fully deterministic and does not require an LLM or paid API access.

Milestone 2 discovers candidate links from YAML-configured trusted sources, applies bounded deterministic traversal, evaluates only pages classified as specific opportunities, and records normalized versions in PostgreSQL. Supabase can provide the managed PostgreSQL database; the application does not depend on Supabase-specific APIs.

Milestone 3 deterministically generates bounded, rotating Match and Discovery searches from the profile, normalizes and filters search results, and feeds candidate pages into the existing evaluation and PostgreSQL lifecycle pipeline. Autonomous search uses a replaceable search-provider boundary, with Tavily as the first production provider. Search provenance is retained separately from the opportunity's original source.

## Design Principles

Opportunity Radar is being built around a few core principles:

- Discover broadly, but prioritize selectively
- Keep factual opportunity data separate from personalized assessment
- Preserve uncertainty instead of guessing
- Use deterministic rules for eligibility, scoring, dates, and ranking where possible
- Keep the user in control of final decisions
- Preserve room for discovery outside obvious existing interests
- Make future LLM-based reasoning optional rather than required

## Architecture

The current system is split into separate stages:

```text
Opportunity URL
      ↓
Fetch + HTML preparation
      ↓
Factual extraction
      ↓
Profile-based evaluation
      ↓
Scoring + eligibility
      ↓
Deduplication
      ↓
Ranked digest
```

Milestone 2 adds a bounded outer loop:

```text
Configured trusted sources
      ↓
Deterministic link discovery (maximum depth 2)
      ↓
Specific opportunity candidates only
      ↓
Milestone 1 extraction and evaluation
      ↓
PostgreSQL state + immutable structured versions
      ↓
New / changed digest
```

Milestone 3 adds a non-recursive search loop:

```text
Profile + preferences
      ↓
Deterministic Match / Discovery queries
      ↓
Replaceable search provider
      ↓
Canonicalization, filtering, deduplication, and caps
      ↓
Milestone 1 extraction and evaluation
      ↓
PostgreSQL opportunity state + search provenance
      ↓
New / changed search digest
```

Open-web search remains deliberately bounded and non-recursive. PostgreSQL stores opportunity history and discovery provenance, while offline tests use replaceable fake providers and an in-memory persistence implementation.
