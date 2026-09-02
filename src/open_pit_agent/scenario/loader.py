"""Load reusable logical Scenario templates from JSON."""
import json
from pathlib import Path
from typing import Any, Dict

from .models import LogicalScenario


def load_logical_scenario(source: Any) -> LogicalScenario:
    """Load a template from a path or mapping and validate its shape."""
    if isinstance(source, (str, Path)):
        raw = json.loads(Path(source).expanduser().read_text(encoding="utf-8"))
    elif isinstance(source, dict):
        raw = dict(source)
    else:
        raise ValueError("scenario template must be a path or object")
    scenario = LogicalScenario.from_dict(raw)
    scenario.validate()
    return scenario
