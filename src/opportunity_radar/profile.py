from pathlib import Path

import yaml

from opportunity_radar.models import OpportunityProfile


def load_profile(path: str | Path) -> OpportunityProfile:
    profile_path = Path(path)
    with profile_path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise ValueError("profile document must contain a mapping")
    return OpportunityProfile.model_validate(data)
