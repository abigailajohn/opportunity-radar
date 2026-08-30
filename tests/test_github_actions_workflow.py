from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "opportunity-radar-daily.yml"
EXPECTED_SECRETS = {
    "TAVILY_API_KEY",
    "OPPORTUNITY_RADAR_DATABASE_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "OPPORTUNITY_RADAR_PROFILE_B64",
}


def load_workflow() -> tuple[str, dict]:
    text = WORKFLOW.read_text(encoding="utf-8")
    return text, yaml.load(text, Loader=yaml.BaseLoader)


def test_daily_workflow_schedule_runtime_and_concurrency() -> None:
    _, workflow = load_workflow()
    assert workflow["on"]["schedule"] == [{"cron": "53 3 * * *"}]
    assert "workflow_dispatch" in workflow["on"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "opportunity-radar-daily",
        "cancel-in-progress": "false",
    }
    job = workflow["jobs"]["run-daily"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == "45"
    assert any(step.get("with", {}).get("python-version") == "3.12" for step in job["steps"])
    assert any(step.get("run") == "python -m scripts.run_daily --mode deterministic" for step in job["steps"])


def test_workflow_uses_only_expected_secret_references() -> None:
    text, _ = load_workflow()
    referenced = {
        name for name in EXPECTED_SECRETS
        if f"${{{{ secrets.{name} }}}}" in text
    }
    assert referenced == EXPECTED_SECRETS
    assert ".env" not in text
    assert "config/sources.yaml" not in text


def test_profile_is_decoded_without_being_printed_or_uploaded() -> None:
    text, workflow = load_workflow()
    steps = workflow["jobs"]["run-daily"]["steps"]
    profile_step = next(step for step in steps if step["name"] == "Reconstruct private profile")
    script = profile_step["run"]
    assert "base64 --decode > config/profile.yaml" in script
    assert "test -s config/profile.yaml" in script
    assert "cat config/profile.yaml" not in script
    assert "upload-artifact" not in text
    cleanup = next(step for step in steps if step["name"] == "Remove reconstructed profile")
    assert cleanup["if"] == "always()"
    assert cleanup["run"] == "rm -f config/profile.yaml"
