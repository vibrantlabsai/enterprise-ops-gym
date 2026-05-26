"""User-simulator system prompt: the static guidelines plus the full scenario.

The guidelines live in the editable ``user_sim_system_prompt.txt`` next to this
module; the scenario is appended to them in full at build time.
"""

import json
from pathlib import Path

_GUIDELINES_PATH = Path(__file__).with_name("user_sim_system_prompt.txt")


def build_user_simulator_prompt(scenario) -> str:
    """Load the user-sim system prompt and append the full scenario."""
    guidelines = _GUIDELINES_PATH.read_text(encoding="utf-8").strip()
    scenario_text = (
        scenario
        if isinstance(scenario, str)
        else json.dumps(scenario, indent=2, ensure_ascii=False)
    )
    return f"{guidelines}\n\n{scenario_text}"
