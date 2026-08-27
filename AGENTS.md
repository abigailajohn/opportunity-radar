# Opportunity Radar — Agent Instructions

## Project goal

Build Opportunity Radar according to the product specification in `docs/`.

The current development target is **Milestone 1 only**:

> 10 manually supplied opportunity URLs -> normalized opportunity facts -> personalized assessments -> ranked digest.

Do not implement future roadmap capabilities unless explicitly requested.

## Source of truth

Documentation is intentionally split across multiple focused files.

For **Milestone 1**, read these first:

- `docs/02-opportunity-evaluation-model.md`
- `docs/03-prioritization-and-scoring.md`
- `docs/04-opportunity-data-model.md`
- `docs/08-mvp-scope.md`
- `docs/milestone-1-implementation-contract.md`
- `config/profile.yaml`

Use `docs/01-foundation.md` for broader product/profile context when needed.

Do **not** implement capabilities from these files unless the current task explicitly asks for them:

- `docs/05-discovery-strategy.md`
- `docs/06-daily-digest-and-notifications.md` beyond Milestone 1 digest generation
- `docs/07-memory-feedback-learning.md`
- `docs/09-future-capabilities-roadmap.md`

## Engineering rules

- Python 3.12+
- Pydantic v2
- Use type hints throughout.
- Keep opportunity facts separate from personalized assessments.
- Missing information must remain unknown; never invent data.
- Prefer deterministic logic for dates, arithmetic, hard eligibility checks, scoring, deduplication, and state transitions.
- Use LLM reasoning only where semantic interpretation is actually required.
- A failure processing one URL must never crash the whole batch.
- Keep external providers behind interfaces so they can be replaced later.
- Do not hard-code personal profile details in Python.
- Do not build a dashboard, scheduler, Telegram integration, WhatsApp integration, automatic discovery, or database persistence during Milestone 1.
- Add tests for all acceptance criteria that can reasonably be automated.

## Development behavior

- Work incrementally.
- Run tests after meaningful changes.
- Do not silently reinterpret or change requirements.
- If implementation and specification conflict, follow the specification.
- Keep the implementation small enough to inspect.
- Do not over-engineer the MVP.
- Before introducing a dependency or architectural component, explain why Milestone 1 needs it.

## Completion

Do not declare Milestone 1 complete until the acceptance criteria in `docs/milestone-1-implementation-contract.md` have been checked individually.
