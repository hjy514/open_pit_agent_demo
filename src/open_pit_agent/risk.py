"""Transparent synthetic slope-risk baseline for the feasibility demo."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .models import Task


LEVELS = ("blue", "yellow", "orange", "red")
LEVEL_RANK = {level: index for index, level in enumerate(LEVELS)}
SLOPE_STATES = (
    "stable",
    "rainfall_infiltration",
    "progressive_deformation",
    "accelerating_deformation",
    "pre_failure",
    "failure",
    "post_failure_monitoring",
)
SLOPE_STATE_BY_STAGE = {
    "normal": "stable",
    "rainfall_infiltration": "rainfall_infiltration",
    "slow_deformation": "progressive_deformation",
    "progressive_deformation": "progressive_deformation",
    "accelerating_deformation": "accelerating_deformation",
    "pre_failure": "pre_failure",
    "failure": "failure",
    "post_action_recheck": "post_failure_monitoring",
    "post_failure_monitoring": "post_failure_monitoring",
}


class RiskConfigError(ValueError):
    """Raised when the synthetic risk scenario is invalid."""


@dataclass(frozen=True)
class RiskObservation:
    sample_id: str
    tick: int
    zone_id: str
    stage: str
    fixed_station: Dict[str, float]
    mobile_equipment: Dict[str, float]
    synthetic: bool = True
    slope_state: str = "stable"

    def metrics(self) -> Dict[str, float]:
        result = dict(self.fixed_station)
        result.update(self.mobile_equipment)
        return result

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RiskAssessment:
    assessment_id: str
    sample_id: str
    tick: int
    zone_id: str
    level: str
    previous_level: str
    trend: str
    reasons: List[str]
    metrics: Dict[str, float]
    model_version: str
    synthetic: bool
    slope_state: str = "stable"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RiskAction:
    trigger_levels: List[str]
    zone_id: str
    task_id_prefix: str
    task_priority: int
    required_capabilities: List[str]
    task_type: str
    work_order_type: str
    auto_review_delay_ticks: int


@dataclass(frozen=True)
class RestrictionAction:
    trigger_levels: List[str]
    restriction_id_prefix: str
    zone_id: str
    road_segment_id: str
    allowed_task_types: List[str]


@dataclass(frozen=True)
class RiskScenario:
    scenario_id: str
    dataset_label: str
    synthetic_data: bool
    model_version: str
    thresholds: Dict[str, Dict[str, float]]
    action: RiskAction
    additional_actions: List[RiskAction]
    restriction: Optional[RestrictionAction]
    observations: List[RiskObservation]


class RuleBasedRiskEngine:
    """Classify observations by explicit per-metric thresholds."""

    def __init__(self, scenario: RiskScenario) -> None:
        self.scenario = scenario
        self._previous_levels: Dict[str, str] = {}

    def assess(self, observation: RiskObservation) -> RiskAssessment:
        metrics = observation.metrics()
        level = "blue"
        matched_by_level: Dict[str, List[str]] = {}
        for candidate in LEVELS[1:]:
            matches = []
            for metric_name, threshold in self.scenario.thresholds[candidate].items():
                value = metrics.get(metric_name)
                if value is not None and value >= threshold:
                    matches.append(
                        "{}={:.3f}>={:.3f}".format(
                            metric_name, value, threshold
                        )
                    )
            matched_by_level[candidate] = matches
            if matches:
                level = candidate

        previous = self._previous_levels.get(observation.zone_id, "blue")
        if LEVEL_RANK[level] > LEVEL_RANK[previous]:
            trend = "rising"
        elif LEVEL_RANK[level] < LEVEL_RANK[previous]:
            trend = "falling"
        else:
            trend = "stable"
        self._previous_levels[observation.zone_id] = level
        reasons = matched_by_level.get(level, [])
        if level == "blue":
            reasons = ["all_metrics_below_yellow_thresholds"]

        return RiskAssessment(
            assessment_id="{}-{}".format(
                self.scenario.scenario_id, observation.sample_id
            ),
            sample_id=observation.sample_id,
            tick=observation.tick,
            zone_id=observation.zone_id,
            level=level,
            previous_level=previous,
            trend=trend,
            reasons=reasons,
            metrics=metrics,
            model_version=self.scenario.model_version,
            synthetic=observation.synthetic,
            slope_state=observation.slope_state,
        )


def create_risk_review_task(
    assessment: RiskAssessment, action: RiskAction
) -> Task:
    return create_risk_task(assessment, action)


def create_risk_task(
    assessment: RiskAssessment, action: RiskAction
) -> Task:
    return Task(
        task_id="{}-{}".format(
            action.task_id_prefix, assessment.assessment_id
        ),
        zone_id=action.zone_id,
        priority=action.task_priority,
        required_capabilities=list(action.required_capabilities),
        status_reason="generated_from_{}_risk".format(assessment.level),
        task_type=action.task_type,
        source_event_id=assessment.assessment_id,
    )


def actions_for_assessment(
    scenario: RiskScenario, assessment: RiskAssessment
) -> List[RiskAction]:
    if LEVEL_RANK[assessment.level] <= LEVEL_RANK[assessment.previous_level]:
        return []
    return [
        action
        for action in [scenario.action] + scenario.additional_actions
        if assessment.level in action.trigger_levels
    ]


def load_risk_scenario(path: Path) -> RiskScenario:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RiskConfigError(
            "Unable to load risk config {}: {}".format(path, exc)
        ) from exc

    try:
        thresholds = {
            level: {
                str(metric): float(value)
                for metric, value in raw["thresholds"][level].items()
            }
            for level in LEVELS[1:]
        }
        action = _parse_action(raw["action"])
        additional_actions = [
            _parse_action(item)
            for item in raw.get("additional_actions", [])
        ]
        restriction_raw = raw.get("restriction")
        restriction = (
            _parse_restriction(restriction_raw)
            if restriction_raw is not None
            else None
        )
        observations = [
            RiskObservation(
                sample_id=str(item["sample_id"]),
                tick=int(item["tick"]),
                zone_id=str(item["zone_id"]),
                stage=str(item["stage"]),
                fixed_station={
                    str(metric): float(value)
                    for metric, value in item["fixed_station"].items()
                },
                mobile_equipment={
                    str(metric): float(value)
                    for metric, value in item["mobile_equipment"].items()
                },
                synthetic=bool(raw["synthetic_data"]),
                slope_state=str(
                    item.get(
                        "slope_state",
                        SLOPE_STATE_BY_STAGE.get(
                            str(item["stage"]), str(item["stage"])
                        ),
                    )
                ),
            )
            for item in raw["observations"]
        ]
        scenario = RiskScenario(
            scenario_id=str(raw["scenario_id"]),
            dataset_label=str(raw["dataset_label"]),
            synthetic_data=bool(raw["synthetic_data"]),
            model_version=str(raw["model_version"]),
            thresholds=thresholds,
            action=action,
            additional_actions=additional_actions,
            restriction=restriction,
            observations=observations,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RiskConfigError(
            "Invalid risk config {}: {}".format(path, exc)
        ) from exc

    _validate_risk_scenario(scenario)
    return scenario


def _validate_risk_scenario(scenario: RiskScenario) -> None:
    if not scenario.synthetic_data:
        raise RiskConfigError(
            "Demo risk config must explicitly declare synthetic_data=true"
        )
    if not scenario.observations:
        raise RiskConfigError("At least one risk observation is required")
    actions = [scenario.action] + scenario.additional_actions
    if any(
        level not in LEVELS
        for action in actions
        for level in action.trigger_levels
    ):
        raise RiskConfigError("Unknown trigger level in risk action")
    ticks = [item.tick for item in scenario.observations]
    if ticks != sorted(ticks) or len(ticks) != len(set(ticks)):
        raise RiskConfigError(
            "Risk observation ticks must be unique and sorted"
        )
    unknown_slope_states = {
        item.slope_state
        for item in scenario.observations
        if item.slope_state not in SLOPE_STATES
    }
    if unknown_slope_states:
        raise RiskConfigError(
            "Unknown slope states: {}".format(
                ", ".join(sorted(unknown_slope_states))
            )
        )
    if any(action.task_priority <= 0 for action in actions):
        raise RiskConfigError("Risk task priority must be positive")
    if any(action.auto_review_delay_ticks < 0 for action in actions):
        raise RiskConfigError(
            "auto_review_delay_ticks cannot be negative"
        )
    prefixes = [action.task_id_prefix for action in actions]
    if len(prefixes) != len(set(prefixes)):
        raise RiskConfigError(
            "Risk action task_id_prefix values must be unique"
        )
    if scenario.restriction is not None:
        if any(
            level not in LEVELS
            for level in scenario.restriction.trigger_levels
        ):
            raise RiskConfigError(
                "Unknown trigger level in restriction action"
            )
        if not scenario.restriction.allowed_task_types:
            raise RiskConfigError(
                "Restriction action must allow at least one task type"
            )


def observations_at_tick(
    observations: Sequence[RiskObservation], tick: int
) -> List[RiskObservation]:
    return [item for item in observations if item.tick == tick]


def create_post_action_feedback_observation(
    scenario: RiskScenario,
    tick: int,
    feedback_id: str,
    zone_id: Optional[str] = None,
) -> RiskObservation:
    """Create an explicitly synthetic post-action recheck for the demo."""

    baseline = scenario.observations[0]
    return RiskObservation(
        sample_id=str(feedback_id),
        tick=int(tick),
        zone_id=str(zone_id or scenario.action.zone_id),
        stage="post_action_recheck",
        fixed_station=dict(baseline.fixed_station),
        mobile_equipment=dict(baseline.mobile_equipment),
        synthetic=True,
        slope_state="post_failure_monitoring",
    )


def _parse_action(raw: Dict[str, Any]) -> RiskAction:
    return RiskAction(
        trigger_levels=[
            str(level) for level in raw["trigger_levels"]
        ],
        zone_id=str(raw["zone_id"]),
        task_id_prefix=str(raw["task_id_prefix"]),
        task_priority=int(raw["task_priority"]),
        required_capabilities=[
            str(value) for value in raw["required_capabilities"]
        ],
        task_type=str(raw.get("task_type", "risk_review")),
        work_order_type=str(raw["work_order_type"]),
        auto_review_delay_ticks=int(
            raw["auto_review_delay_ticks"]
        ),
    )


def _parse_restriction(
    raw: Dict[str, Any]
) -> RestrictionAction:
    return RestrictionAction(
        trigger_levels=[
            str(level) for level in raw["trigger_levels"]
        ],
        restriction_id_prefix=str(
            raw["restriction_id_prefix"]
        ),
        zone_id=str(raw["zone_id"]),
        road_segment_id=str(raw["road_segment_id"]),
        allowed_task_types=[
            str(value) for value in raw["allowed_task_types"]
        ],
    )
