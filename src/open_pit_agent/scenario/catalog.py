"""Single-source Scenario Catalog with legacy-config compatibility."""
import json
from pathlib import Path
from typing import Dict, Iterable, Optional

from .models import ScenarioSpec


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CATALOG_PATH = PROJECT_ROOT / "configs" / "scenario_catalog.json"
REQUIRED_FIELDS = (
    "scenario_id", "name", "type", "description", "compatibility_config",
    "fleet", "randomization", "constraints", "events", "success_criteria",
    "termination", "modes", "carla_readiness", "supported_policies",
)


def load_scenario_catalog(path: Optional[Path] = None) -> Dict[str, Dict[str, object]]:
    """Load and validate catalog metadata without loading scenario runtime config."""
    source = Path(path or DEFAULT_CATALOG_PATH)
    raw = json.loads(source.read_text(encoding="utf-8"))
    if raw.get("schema_version") != "openpit.scenario-catalog.v1":
        raise ValueError("unsupported scenario catalog schema")
    entries = raw.get("scenarios")
    if not isinstance(entries, list) or not entries:
        raise ValueError("scenario catalog requires non-empty scenarios")
    catalog = {}
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            raise ValueError("scenario catalog entry must be an object")
        missing = [key for key in REQUIRED_FIELDS if key not in raw_entry]
        if missing:
            raise ValueError("scenario catalog entry missing: {}".format(", ".join(missing)))
        scenario_id = str(raw_entry["scenario_id"]).lower()
        if scenario_id in catalog:
            raise ValueError("duplicate scenario_id: {}".format(scenario_id))
        fleet = raw_entry["fleet"]
        counts = fleet.get("selectable_vehicle_counts") if isinstance(fleet, dict) else None
        if not isinstance(counts, list) or not counts or any(int(value) < 1 for value in counts):
            raise ValueError("scenario {} has invalid selectable vehicle counts".format(scenario_id))
        modes = raw_entry["modes"]
        if not isinstance(modes, list) or not set(modes).issubset({"structural", "carla"}):
            raise ValueError("scenario {} has invalid modes".format(scenario_id))
        config_path = PROJECT_ROOT / str(raw_entry["compatibility_config"])
        if not config_path.is_file():
            raise ValueError("scenario {} compatibility config missing: {}".format(scenario_id, config_path))
        entry = dict(raw_entry)
        entry["scenario_id"] = scenario_id
        entry["compatibility_config_path"] = str(config_path)
        entry["status"] = "IMPLEMENTED"
        catalog[scenario_id] = entry
    return catalog


def compatibility_config_path(scenario_id: str,
                              catalog: Optional[Dict[str, Dict[str, object]]] = None
                              ) -> Path:
    """Return the existing runtime configuration used by a catalog entry."""
    entries = catalog or load_scenario_catalog()
    key = str(scenario_id).lower()
    if key not in entries:
        raise ValueError("unknown scenario: {}".format(scenario_id))
    return Path(entries[key]["compatibility_config_path"])


def selectable_vehicle_counts(scenario_id: str,
                              catalog: Optional[Dict[str, Dict[str, object]]] = None
                              ) -> Iterable[int]:
    entries = catalog or load_scenario_catalog()
    key = str(scenario_id).lower()
    if key not in entries:
        raise ValueError("unknown scenario: {}".format(scenario_id))
    return tuple(int(value) for value in entries[key]["fleet"]["selectable_vehicle_counts"])


def scenario_spec(
    scenario_id: str,
    catalog: Optional[Dict[str, Dict[str, object]]] = None,
) -> ScenarioSpec:
    """Return the common immutable ScenarioSpec for any S01-S09 entry."""
    entries = catalog or load_scenario_catalog()
    key = str(scenario_id).lower()
    if key not in entries:
        raise ValueError("unknown scenario: {}".format(scenario_id))
    return ScenarioSpec.from_catalog_entry(key, entries[key])


def validate_scenario_request(scenario_id: str, mode: str, vehicle_count: int,
                              catalog: Optional[Dict[str, Dict[str, object]]] = None
                              ) -> None:
    """Fail closed when the requested catalog combination is unsupported."""
    entries = catalog or load_scenario_catalog()
    key, mode = str(scenario_id).lower(), str(mode).lower()
    if key not in entries:
        raise ValueError("unknown scenario: {}".format(scenario_id))
    entry = entries[key]
    if mode not in entry["modes"]:
        raise ValueError("scenario {} does not support mode {}".format(key, mode))
    allowed = tuple(int(value) for value in entry["fleet"]["selectable_vehicle_counts"])
    if int(vehicle_count) not in allowed:
        raise ValueError("scenario {} supports vehicle_count {}; requested={}".format(
            key, list(allowed), vehicle_count
        ))
