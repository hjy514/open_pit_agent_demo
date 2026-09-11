"""Run-scoped evidence logging for reports and regression tests."""

import json
import warnings
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable
from uuid import uuid4

from .sqlite_store import SqliteRunStore


class EvidenceRecorder:
    def __init__(self, root: Path, scenario_id: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = "{}-{}-{}".format(scenario_id, timestamp, uuid4().hex[:8])
        self.run_dir = Path(root).expanduser().resolve() / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.events_path = self.run_dir / "events.jsonl"
        self.database_path = self._default_database_path(Path(root))
        self.store = self._open_store(scenario_id)

    @staticmethod
    def _default_database_path(root: Path) -> Path:
        """Place runtime data beside the project, not inside artifacts."""
        resolved_root = Path(root).expanduser().resolve()
        project_root = (
            resolved_root.parents[1]
            if len(resolved_root.parents) >= 2
            else resolved_root.parent
        )
        # Some legacy callers pass a relative ``artifacts/runs`` path while
        # their process cwd is ``/``. Never turn that into a system ``/data``
        # directory; use this package's project root in that compatibility
        # case instead.
        if project_root == Path("/"):
            project_root = Path(__file__).resolve().parents[2]
        return project_root / "data" / "database" / "openpit.db"

    def _open_store(self, scenario_id: str):
        try:
            store = SqliteRunStore(self.database_path)
            store.start_run(self.run_id, scenario_id)
            return store
        except Exception as exc:  # pragma: no cover - safety fallback
            warnings.warn(
                "SQLite evidence sidecar unavailable; JSON evidence remains "
                "enabled: {}".format(exc),
                RuntimeWarning,
            )
            return None

    def _store(self, operation) -> None:
        if self.store is None:
            return
        try:
            operation(self.store)
        except Exception as exc:  # pragma: no cover - safety fallback
            warnings.warn(
                "SQLite evidence write failed; JSON evidence remains enabled: "
                "{}".format(exc),
                RuntimeWarning,
            )

    def record(self, event_type: str, payload: Dict[str, Any]) -> None:
        item = {
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "payload": payload,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        self._store(
            lambda store: store.record_event(
                self.run_id,
                item["timestamp"],
                event_type,
                payload,
            )
        )

    def record_episode(self, episode: Any, config_path: Path = None) -> None:
        """Index a resolved episode without affecting CARLA execution."""

        self._store(
            lambda store: store.record_episode(episode, config_path=config_path)
        )

    def record_unified_scenario_contract(
        self, result: Dict[str, Any], config_path: Path = None
    ) -> int:
        """Persist the common S01-S09 episode envelope in existing tables."""
        episode = result.get("generated_episode")
        spec = result.get("scenario_spec")
        if not isinstance(episode, dict) or not isinstance(spec, dict):
            return 0
        self.write_json("scenario_contract.json", {
            "data_contract": result.get("data_contract", {}),
            "scenario_spec": spec,
            "generated_episode": episode,
            "concrete_episode_v2": result.get("concrete_episode_v2", {}),
            "production_runtime": result.get("production_runtime", {}),
            "scenario_events": result.get("scenario_events", []),
            "event_timeline": result.get("event_timeline", {}),
            "decisions": result.get("decisions", []),
            "decision_points": result.get("decision_points", []),
            "route_plans": result.get("route_plans", []),
            "task_results": result.get("task_results", []),
            "metrics": result.get("metrics", {}),
            "closed_loop_validation": result.get("closed_loop_validation", {}),
        })

        def item_value(item, *names, default=None):
            for name in names:
                if item.get(name) is not None:
                    return item[name]
            return default

        vehicles = []
        for item in episode.get("vehicles", []):
            if not isinstance(item, dict) or not item.get("vehicle_id"):
                continue
            role = str(item_value(item, "initial_role", "role", "role_name",
                                  default="unassigned"))
            status = str(item_value(item, "initial_status", "status",
                                    "task_status", default="unknown"))
            vehicles.append(SimpleNamespace(
                vehicle_id=str(item["vehicle_id"]),
                display_name=str(item_value(item, "display_name",
                                            default=item["vehicle_id"])),
                equipment_type=str(item_value(item, "equipment_type",
                                               default=role)),
                blueprint=str(item_value(item, "blueprint",
                                         default="NOT_AVAILABLE")),
                capabilities=list(item.get("capabilities") or []),
                spawn_point_index=item.get("spawn_point_index"),
                initial_role=role, initial_status=status,
                available=bool(item.get("available", True)),
                active=bool(item.get("active", True)),
                in_traffic=bool(item.get("in_traffic", False)),
            ))
        tasks = []
        for item in episode.get("tasks", []):
            if not isinstance(item, dict) or not item.get("task_id"):
                continue
            tasks.append(SimpleNamespace(
                task_id=str(item["task_id"]),
                zone_id=item.get("zone_id"),
                task_type=item.get("task_type"),
                priority=item.get("priority"),
                required_capabilities=list(item.get("required_capabilities") or []),
                preferred_vehicle_id=item.get("preferred_vehicle_id"),
            ))
        events = []
        for item in episode.get("events", []):
            if not isinstance(item, dict) or not item.get("event_id"):
                continue
            trigger = item.get("trigger") if isinstance(item.get("trigger"), dict) else {}
            events.append(SimpleNamespace(
                event_id=str(item["event_id"]),
                event_type=str(item.get("event_type") or "scenario_event"),
                trigger_tick=trigger.get("tick"),
                target_vehicle_id=item.get("payload", {}).get("vehicle_id")
                    if isinstance(item.get("payload"), dict) else None,
                parameters=dict(item.get("payload") or {}),
            ))
        fleet = dict(episode.get("fleet") or {})
        total = int(fleet.get("total") or fleet.get("total_vehicles") or len(vehicles))
        fleet_snapshot = {
            "total_vehicles": total,
            "available_vehicles": int(fleet.get("available") or fleet.get("available_vehicles") or total),
            "active_vehicles": int(fleet.get("active") or fleet.get("active_vehicles") or total),
            "traffic_vehicles": int(fleet.get("traffic") or fleet.get("traffic_vehicles") or 0),
            "task_load": fleet.get("task_load"),
            "traffic_density": fleet.get("traffic_density"),
        }
        snapshot = SimpleNamespace(
            run_id=self.run_id,
            scenario_id=str(episode.get("scenario_id") or result.get("scenario_id")),
            scenario_name=str(spec.get("name") or episode.get("scenario_id")),
            scenario_version=str(spec.get("schema_version") or "openpit.scenario-spec.v1"),
            seed=episode.get("seed"), fleet_snapshot=fleet_snapshot,
            vehicles=vehicles, tasks=tasks, events=events,
            to_dict=lambda: dict(episode),
        )
        self._store(lambda store: store.record_episode(
            snapshot, config_path=config_path,
            policy_version=result.get("provenance", {}).get("policy_version"),
            route_planner_version=result.get("provenance", {}).get("route_planner_version"),
            risk_model_version=result.get("provenance", {}).get("risk_model_version"),
        ))
        # Keep the decision boundary queryable before a future UI adds human
        # responses.  This is a declaration of a recommendation/review point,
        # not evidence that an operator approved a structural result.
        for point in result.get("decision_points", []):
            if not isinstance(point, dict) or not point.get("decision_point_id"):
                continue
            self.record("decision_point_created", dict(point))
        production = result.get("production_runtime")
        if isinstance(production, dict):
            for transition in production.get("transitions", []):
                if isinstance(transition, dict):
                    self.record(
                        "task_production_stage_changed", dict(transition)
                    )
        return 1

    def record_runtime_events(self, result: Dict[str, Any]) -> int:
        """Persist simulator feedback already emitted by an execution adapter.

        The execution layer remains storage-agnostic.  This method only copies
        observed runtime facts into the common JSONL/SQLite evidence stream;
        it does not synthesize task completion or other simulator outcomes.
        """

        record_count = 0
        events = result.get("events", [])
        if not isinstance(events, list):
            return record_count
        for event in events:
            if not isinstance(event, dict) or not event.get("event_type"):
                continue
            payload = event.get("payload", {})
            payload = dict(payload) if isinstance(payload, dict) else {
                "raw_payload": payload
            }
            if event.get("tick") is not None:
                payload.setdefault("tick", event.get("tick"))
            payload.setdefault("simulator_mode", result.get("mode"))
            self.record(str(event["event_type"]), payload)
            record_count += 1
        return record_count

    def record_execution_feedback(self, result: Dict[str, Any]) -> int:
        """Persist standard adapter feedback without changing its meaning."""
        records = result.get("execution_feedback", [])
        if not isinstance(records, list):
            return 0
        count = 0
        for feedback in records:
            if not isinstance(feedback, dict):
                continue
            self.record("execution_feedback", dict(feedback))
            count += 1
        return count

    def record_decision_experiences(self, result: Dict[str, Any]) -> int:
        """Persist measured decision-boundary samples without inventing reward.

        The compact JSONL artifact is the future dataset source. The same
        payload is indexed in the existing event stream, so this first data
        contract needs neither a new database table nor a schema migration.
        """
        experiences = [
            dict(item) for item in result.get("decision_experiences", [])
            if isinstance(item, dict) and item.get("experience_id")
        ]
        if not experiences:
            return 0
        self.write_jsonl("decision_experiences.jsonl", experiences)
        for item in experiences:
            self.record("decision_experience_captured", item)
        return len(experiences)

    def record_closed_loop_cycle(self, result: Dict[str, Any]) -> int:
        """Save the common cycle as JSON evidence and a queryable DB row."""
        cycle = result.get("closed_loop_cycle")
        if not isinstance(cycle, dict):
            return 0
        self.write_json("closed_loop_cycle.json", cycle)
        self._store(lambda store: store.record_closed_loop_cycle(
            self.run_id, str(result.get("scenario_key") or "unknown"), cycle
        ))
        return 1

    def record_policy_comparison(self, result: Dict[str, Any]) -> int:
        """Record executed-baseline and shadow-policy decisions from a run result.

        This is an evidence transformation only.  It never changes the
        selected vehicle or sends an action to CARLA.
        """

        comparison = result.get("policy_comparison", {})
        if not isinstance(comparison, dict) or not comparison:
            return 0
        executed_policy = comparison.get("executed_policy")
        shadow_policy = comparison.get("shadow_policy")
        safety_context = comparison.get("safety_shield", {})
        if not isinstance(safety_context, dict):
            safety_context = {}
        safety_by_task = {
            str(item.get("task_id")): item
            for item in safety_context.get("reviews", [])
            if isinstance(item, dict) and item.get("task_id")
        }
        assignments = {
            str(item.get("task_id")): item
            for item in result.get("assignments", [])
            if isinstance(item, dict) and item.get("task_id")
        }
        baseline_rankings = result.get("candidate_rankings", {})
        record_count = 0

        for task_id, assignment in sorted(assignments.items()):
            candidates = baseline_rankings.get(task_id, [])
            self.record(
                "agent_decision",
                {
                    "decision_id": "{}:executed-baseline".format(task_id),
                    "context": "structural_baseline_execution",
                    "task_id": task_id,
                    "execution_mode": result.get("mode"),
                    "scheduler_agent_action": {
                        "assigned_vehicle_id": assignment.get("vehicle_id"),
                        "score": assignment.get("score"),
                        "reason": assignment.get("reason"),
                        "policy_version": executed_policy,
                    },
                    "candidate_evaluations": candidates,
                    "safety_review": (
                        safety_by_task.get(task_id)
                        if safety_context.get("mode") == "execution_gate" else None
                    ),
                },
            )
            record_count += 1

        items = comparison.get("comparisons", [])
        if not isinstance(items, list):
            items = []
        for item in items:
            if not isinstance(item, dict) or not item.get("task_id"):
                continue
            task_id = str(item["task_id"])
            selected_vehicle = item.get("shadow_selected_vehicle_v1")
            candidate_evaluations = []
            selected_cost = None
            for candidate in item.get("v1_candidate_ranking", []):
                if not isinstance(candidate, dict) or not candidate.get("vehicle_id"):
                    continue
                payload = dict(candidate)
                payload["score"] = candidate.get("total_cost")
                failed = [
                    check.get("constraint")
                    for check in candidate.get("constraint_results", [])
                    if isinstance(check, dict) and not check.get("passed")
                ]
                payload["reason"] = (
                    "hard_constraints_passed"
                    if not failed
                    else "hard_constraints_rejected:{}".format(",".join(failed))
                )
                candidate_evaluations.append(payload)
                if candidate.get("vehicle_id") == selected_vehicle:
                    selected_cost = candidate.get("total_cost")
            self.record(
                "agent_decision",
                {
                    "decision_id": "{}:shadow-multi-objective".format(task_id),
                    "context": "structural_shadow_policy_comparison",
                    "task_id": task_id,
                    "executed_vehicle_id": item.get("executed_vehicle_v0"),
                    "selection_changed": item.get("selection_changed"),
                    "shadow_only": True,
                    "optimizer_version": comparison.get("optimizer_version"),
                    "scheduler_agent_action": {
                        "assigned_vehicle_id": selected_vehicle,
                        "score": selected_cost,
                        "policy_version": shadow_policy,
                    },
                    "candidate_evaluations": candidate_evaluations,
                    "safety_review": safety_by_task.get(task_id),
                    "safety_evaluation_mode": safety_context.get("mode"),
                },
            )
            record_count += 1
        return record_count

    def record_unified_runtime_plan(self, result: Dict[str, Any]) -> Dict[str, int]:
        """Persist normalized decisions and CAR/route plans from CARLA runs.

        The unified scenario runner already returns simulator-neutral
        ``decisions`` and structural ``route_plans``.  CARLA additionally
        returns one ``task_mission_plan`` per vehicle.  This method indexes
        those existing facts; it neither creates a decision nor changes a
        route used by the simulator.
        """

        decision_count = 0
        for decision in result.get("decisions", []):
            if not isinstance(decision, dict) or not decision.get("decision_id"):
                continue
            self.record("agent_decision", {
                "decision_id": str(decision["decision_id"]),
                "context": str(
                    decision.get("decision_type") or "unified_scenario_decision"
                ),
                "task_id": decision.get("task_id"),
                "tick": decision.get("tick"),
                "source": "openpit.decision-record.v1",
                "scheduler_agent_action": {
                    "action_type": decision.get("action_type"),
                    "assigned_vehicle_id": decision.get("selected_vehicle_id"),
                    "score": decision.get("score"),
                    "policy_version": decision.get("policy_version"),
                    "reason": decision.get("reason"),
                },
                "candidate_evaluations": decision.get(
                    "candidate_evaluations", []
                ),
                "constraint_results": decision.get("constraint_results", {}),
                "safety_review": decision.get("safety_review"),
                "route_contract": decision.get("route_contract"),
                "native_payload": decision.get("native_payload"),
            })
            decision_count += 1

        route_count = 0
        for route_plan in result.get("route_plans", []):
            if not isinstance(route_plan, dict) or not route_plan.get(
                "route_plan_id"
            ):
                continue
            self._store(
                lambda store, item=dict(route_plan): store.record_route_plan(
                    self.run_id, item
                )
            )
            route_count += 1

        mission_count = 0
        for mission in result.get("task_mission_plans", []):
            if not isinstance(mission, dict) or not mission.get("task_id"):
                continue
            task_id = str(mission["task_id"])
            route_record = dict(mission)
            route_record.update({
                "route_plan_id": "{}:carla-mission".format(task_id),
                "planner_version": "CARLA_BASIC_AGENT_EXECUTION_BRIDGE_V1",
                "start_node": mission.get("vehicle_spawn_point_id"),
                "goal_node": mission.get("service_target_point_id"),
                "distance_m": (
                    float(mission.get("deadhead_route_length_m") or 0.0)
                    + float(mission.get("mission_route_length_m") or 0.0)
                ),
                "status": "CARLA_MISSION_CONFIGURED",
                "route_record_type": "task_mission_plan",
                "validation_status": mission.get("mission_route_evidence"),
            })
            self._store(
                lambda store, item=route_record: store.record_route_plan(
                    self.run_id, item
                )
            )
            mission_count += 1

        return {
            "decision_count": decision_count,
            "route_plan_count": route_count,
            "task_mission_plan_count": mission_count,
        }

    def record_structural_events(self, result: Dict[str, Any]) -> int:
        """Persist explicit structural facts already present in a run result."""

        compound_events = result.get("compound_events")
        if isinstance(compound_events, list) and len(compound_events) >= 2:
            parameters = result.get("compound_event_parameters", {})
            road_tick = parameters.get("road_closure_tick")
            failure_tick = parameters.get("vehicle_failure_tick")
            recovery_tick = parameters.get("recovery_tick")
            closed_edge_id = result.get("closed_edge_id")
            self.record("road_closed", {
                "tick": road_tick, "road_id": closed_edge_id,
                "status": "CLOSED", "compound_stage": 1,
                "source": "parameterized_synthetic_scenario",
            })
            record_count = 1
            for route_plan in result.get("route_plans", []):
                if isinstance(route_plan, dict):
                    self._store(
                        lambda store, item=route_plan: store.record_route_plan(
                            self.run_id, item
                        )
                    )
            for change in result.get("route_changes", []):
                if not isinstance(change, dict):
                    continue
                self.record("agent_decision", {
                    "decision_id": "{}:compound-road-response".format(
                        change.get("task_id")),
                    "context": "compound_stage_1_road_closure",
                    "task_id": change.get("task_id"),
                    "scheduler_agent_action": {
                        "action_type": change.get("action_type"),
                        "assigned_vehicle_id": change.get("vehicle_id"),
                        "policy_version": result.get("route_planner_version"),
                        "reason": "closed_edge_avoided",
                    },
                    "closed_edge_id": closed_edge_id,
                    "candidate_evaluations": change.get(
                        "candidate_evaluations", []),
                    "constraint_results": change.get("constraint_results", {}),
                    "safety_review": change.get("safety_review"),
                    "route_contract": change.get("route_contract"),
                })
                self.record("route_replanned", {
                    "tick": road_tick, "task_id": change.get("task_id"),
                    "vehicle_id": change.get("vehicle_id"),
                    "closed_edge_id": closed_edge_id,
                    "action_type": change.get("action_type"),
                    "status": "planned", "compound_stage": 1,
                })
                record_count += 2
            self.record("vehicle_fault", {
                "tick": failure_tick,
                "vehicle_id": result.get("failed_vehicle_id"),
                "status": "fault", "compound_stage": 2,
                "source": "parameterized_synthetic_scenario",
            })
            record_count += 1
            for task_id in result.get("released_task_ids", []):
                self.record("task_released", {
                    "tick": failure_tick, "task_id": task_id,
                    "reason": "vehicle_fault_during_active_road_closure",
                    "compound_stage": 2,
                })
                record_count += 1
            for decision in result.get("compound_failure_decisions", []):
                if not isinstance(decision, dict):
                    continue
                self.record("agent_decision", {
                    "decision_id": "{}:compound-fault-takeover".format(
                        decision.get("task_id")),
                    "context": "compound_stage_2_vehicle_failure",
                    "task_id": decision.get("task_id"),
                    "scheduler_agent_action": {
                        "action_type": decision.get("action_type"),
                        "assigned_vehicle_id": decision.get("selected_vehicle_id"),
                        "policy_version": decision.get("policy_version"),
                        "reason": "safe_capable_candidate_during_road_closure",
                    },
                    "failed_vehicle_id": decision.get("failed_vehicle_id"),
                    "closed_edge_id": closed_edge_id,
                    "constraint_results": decision.get("constraint_results", {}),
                    "safety_review": decision.get("safety_review"),
                    "route_contract": decision.get("route_contract"),
                    "candidate_evaluations": decision.get(
                        "candidate_evaluations", []),
                })
                self.record("task_reassigned", {
                    "tick": failure_tick, "task_id": decision.get("task_id"),
                    "original_vehicle_id": decision.get("failed_vehicle_id"),
                    "vehicle_id": decision.get("selected_vehicle_id"),
                    "reason": "vehicle_fault_during_active_road_closure",
                    "status": "assigned", "compound_stage": 2,
                })
                record_count += 2
            for task in result.get("tasks", []):
                if not isinstance(task, dict) or not task.get("task_id"):
                    continue
                self.record("task_completed", {
                    "tick": task.get("completed_tick"),
                    "task_id": task.get("task_id"), "status": task.get("status"),
                    "reason": task.get("status_reason"),
                })
                record_count += 1
            self.record("road_reopened", {
                "tick": recovery_tick, "road_id": closed_edge_id,
                "status": "OPEN",
            })
            self.record("run_completed", {
                "tick": recovery_tick, "status": result.get("status"),
                "mode": result.get("mode"),
            })
            return record_count + 2

        blast_event = result.get("blast_event")
        if isinstance(blast_event, dict) and blast_event:
            notice_tick = blast_event.get("notice_tick")
            start_tick = blast_event.get("blast_start_tick")
            clearance_tick = blast_event.get("clearance_tick")
            restricted_edge_id = result.get("restricted_edge_id")
            self.record("blast_announced", dict(blast_event))
            self.record("blast_control_activated", {
                "tick": start_tick, "road_id": restricted_edge_id,
                "status": "CONTROLLED",
                "exclusion_scope": blast_event.get("exclusion_scope"),
                "source": "parameterized_synthetic_scenario",
            })
            record_count = 2
            for route_plan in result.get("route_plans", []):
                if isinstance(route_plan, dict):
                    self._store(
                        lambda store, item=route_plan: store.record_route_plan(
                            self.run_id, item
                        )
                    )
            for decision in result.get("blast_decisions", []):
                if not isinstance(decision, dict):
                    continue
                self.record("agent_decision", {
                    "decision_id": "{}:blast-control".format(
                        decision.get("task_id")),
                    "context": "planned_blast_temporary_road_control",
                    "task_id": decision.get("task_id"),
                    "scheduler_agent_action": {
                        "action_type": decision.get("action_type"),
                        "assigned_vehicle_id": decision.get("vehicle_id"),
                        "policy_version": decision.get("policy_version"),
                        "reason": decision.get("reason"),
                    },
                    "restricted_edge_id": restricted_edge_id,
                    "wait_until_tick": decision.get("wait_until_tick"),
                    "measurement_status": decision.get("measurement_status"),
                    "constraint_results": decision.get("constraint_results", {}),
                    "safety_review": decision.get("safety_review"),
                    "route_contract": decision.get("route_contract"),
                    "candidate_evaluations": decision.get(
                        "candidate_evaluations", []),
                })
                response_type = (
                    "vehicle_held_for_blast"
                    if decision.get("action_type") == "hold_until_blast_clearance"
                    else "blast_safe_route_planned"
                )
                self.record(response_type, {
                    "tick": notice_tick, "task_id": decision.get("task_id"),
                    "vehicle_id": decision.get("vehicle_id"),
                    "road_id": restricted_edge_id,
                    "wait_until_tick": decision.get("wait_until_tick"),
                    "status": "planned",
                })
                record_count += 2
            for task in result.get("tasks", []):
                if not isinstance(task, dict) or not task.get("task_id"):
                    continue
                self.record("task_completed", {
                    "tick": task.get("completed_tick"),
                    "task_id": task.get("task_id"), "status": task.get("status"),
                    "reason": task.get("status_reason"),
                })
                record_count += 1
            self.record("blast_area_cleared", {
                "tick": clearance_tick, "road_id": restricted_edge_id,
                "status": "CLEARED",
            })
            self.record("road_reopened", {
                "tick": clearance_tick, "road_id": restricted_edge_id,
                "status": "OPEN",
            })
            self.record("run_completed", {
                "tick": clearance_tick, "status": result.get("status"),
                "mode": result.get("mode"),
            })
            return record_count + 3

        equipment_event = result.get("equipment_event")
        if isinstance(equipment_event, dict) and equipment_event:
            failure_tick = equipment_event.get("failure_tick")
            recovery_tick = equipment_event.get("recovery_tick")
            self.record("equipment_fault", dict(equipment_event))
            record_count = 1
            for task_id in result.get("affected_task_ids", []):
                self.record("task_paused", {
                    "tick": failure_tick, "task_id": task_id,
                    "equipment_id": equipment_event.get("equipment_id"),
                    "reason": "assigned_loading_equipment_unavailable",
                })
                record_count += 1
            for route_plan in result.get("route_plans", []):
                if isinstance(route_plan, dict):
                    self._store(
                        lambda store, item=route_plan: store.record_route_plan(
                            self.run_id, item
                        )
                    )
            for decision in result.get("equipment_decisions", []):
                if not isinstance(decision, dict):
                    continue
                self.record("agent_decision", {
                    "decision_id": "{}:equipment-switch".format(
                        decision.get("task_id")),
                    "context": "loading_equipment_failure_work_point_switch",
                    "task_id": decision.get("task_id"),
                    "scheduler_agent_action": {
                        "action_type": decision.get("action_type"),
                        "assigned_vehicle_id": decision.get("vehicle_id"),
                        "policy_version": decision.get("policy_version"),
                        "reason": "failed_equipment_excluded_and_alternative_reachable",
                    },
                    "failed_equipment_id": decision.get("failed_equipment_id"),
                    "alternative_equipment_id": decision.get(
                        "alternative_equipment_id"),
                    "failed_work_point_id": decision.get("failed_work_point_id"),
                    "alternative_work_point_id": decision.get(
                        "alternative_work_point_id"),
                    "estimated_task_delay_s": decision.get(
                        "estimated_task_delay_s"),
                    "measurement_status": decision.get("measurement_status"),
                    "constraint_results": decision.get("constraint_results", {}),
                    "safety_review": decision.get("safety_review"),
                    "route_contract": decision.get("route_contract"),
                    "candidate_evaluations": [],
                })
                self.record("task_work_point_switched", {
                    "tick": failure_tick, "task_id": decision.get("task_id"),
                    "vehicle_id": decision.get("vehicle_id"),
                    "from_point_id": decision.get("failed_work_point_id"),
                    "to_point_id": decision.get("alternative_work_point_id"),
                    "status": "planned",
                })
                record_count += 2
            for task in result.get("tasks", []):
                if not isinstance(task, dict) or not task.get("task_id"):
                    continue
                self.record("task_completed", {
                    "tick": task.get("completed_tick"),
                    "task_id": task.get("task_id"), "status": task.get("status"),
                    "reason": task.get("status_reason"),
                })
                record_count += 1
            self.record("equipment_recovered", {
                "tick": recovery_tick,
                "equipment_id": equipment_event.get("equipment_id"),
                "status": "AVAILABLE",
            })
            self.record("run_completed", {
                "tick": recovery_tick, "status": result.get("status"),
                "mode": result.get("mode"),
            })
            return record_count + 2

        congestion_event = result.get("congestion_event")
        if isinstance(congestion_event, dict) and congestion_event:
            event_tick = congestion_event.get("event_tick")
            recovery_tick = congestion_event.get("recovery_tick")
            bottleneck_edge_id = result.get("bottleneck_edge_id")
            self.record("congestion_detected", dict(congestion_event, road_id=bottleneck_edge_id))
            self.record("traffic_control_activated", {
                "tick": event_tick, "road_id": bottleneck_edge_id,
                "capacity": congestion_event.get("road_capacity_vehicles"),
                "minimum_safety_headway_seconds": congestion_event.get(
                    "minimum_safety_headway_seconds"),
                "source": "parameterized_synthetic_scenario",
            })
            record_count = 2
            for route_plan in result.get("route_plans", []):
                if isinstance(route_plan, dict):
                    self._store(
                        lambda store, item=route_plan: store.record_route_plan(
                            self.run_id, item
                        )
                    )
            for decision in result.get("traffic_decisions", []):
                if not isinstance(decision, dict):
                    continue
                self.record("agent_decision", {
                    "decision_id": "{}:traffic-control".format(
                        decision.get("task_id")),
                    "context": "shared_road_capacity_safe_entry",
                    "task_id": decision.get("task_id"),
                    "scheduler_agent_action": {
                        "action_type": decision.get("action_type"),
                        "assigned_vehicle_id": decision.get("vehicle_id"),
                        "policy_version": decision.get("policy_version"),
                        "reason": "capacity_one_eta_priority_headway_schedule",
                    },
                    "bottleneck_edge_id": bottleneck_edge_id,
                    "estimated_arrival_s": decision.get("estimated_arrival_s"),
                    "scheduled_entry_s": decision.get("scheduled_entry_s"),
                    "scheduled_exit_s": decision.get("scheduled_exit_s"),
                    "estimated_wait_s": decision.get("estimated_wait_s"),
                    "measurement_status": decision.get("measurement_status"),
                    "constraint_results": decision.get("constraint_results", {}),
                    "safety_review": decision.get("safety_review"),
                    "route_contract": decision.get("route_contract"),
                    "candidate_evaluations": [],
                })
                response_type = (
                    "vehicle_held" if decision.get("action_type")
                    == "hold_for_safe_headway" else "bottleneck_entry_authorized"
                )
                self.record(response_type, {
                    "tick": event_tick, "task_id": decision.get("task_id"),
                    "vehicle_id": decision.get("vehicle_id"),
                    "road_id": bottleneck_edge_id,
                    "scheduled_entry_s": decision.get("scheduled_entry_s"),
                    "estimated_wait_s": decision.get("estimated_wait_s"),
                    "status": "scheduled",
                })
                record_count += 2
            for task in result.get("tasks", []):
                if not isinstance(task, dict) or not task.get("task_id"):
                    continue
                self.record("task_completed", {
                    "tick": task.get("completed_tick"),
                    "task_id": task.get("task_id"), "status": task.get("status"),
                    "reason": task.get("status_reason"),
                })
                record_count += 1
            self.record("congestion_cleared", {
                "tick": recovery_tick, "road_id": bottleneck_edge_id,
                "status": "CLEARED",
            })
            self.record("traffic_control_released", {
                "tick": recovery_tick, "road_id": bottleneck_edge_id,
                "status": "RELEASED",
            })
            self.record("run_completed", {
                "tick": recovery_tick, "status": result.get("status"),
                "mode": result.get("mode"),
            })
            return record_count + 3

        weather_event = result.get("weather_event")
        if isinstance(weather_event, dict) and weather_event:
            event_tick = weather_event.get("event_tick")
            recovery_tick = weather_event.get("recovery_tick")
            degraded_edge_id = result.get("degraded_edge_id")
            self.record("weather_started", dict(weather_event))
            self.record("road_restricted", {
                "tick": event_tick, "road_id": degraded_edge_id,
                "status": "RESTRICTED",
                "restricted_speed_factor": weather_event.get(
                    "restricted_speed_factor"
                ),
                "source": "parameterized_synthetic_scenario",
            })
            record_count = 2
            for route_plan in result.get("route_plans", []):
                if isinstance(route_plan, dict):
                    self._store(
                        lambda store, item=route_plan: store.record_route_plan(
                            self.run_id, item
                        )
                    )
            for decision in result.get("weather_decisions", []):
                if not isinstance(decision, dict):
                    continue
                self.record("agent_decision", {
                    "decision_id": "{}:weather-response".format(
                        decision.get("task_id")
                    ),
                    "context": "extreme_weather_road_capacity_response",
                    "task_id": decision.get("task_id"),
                    "scheduler_agent_action": {
                        "action_type": decision.get("action_type"),
                        "assigned_vehicle_id": decision.get("vehicle_id"),
                        "policy_version": decision.get("policy_version"),
                        "reason": "minimum_estimated_safe_travel_time",
                    },
                    "degraded_edge_id": degraded_edge_id,
                    "baseline_eta_s": decision.get("baseline_eta_s"),
                    "selected_eta_s": decision.get("selected_eta_s"),
                    "estimated_delay_s": decision.get("estimated_delay_s"),
                    "measurement_status": decision.get("measurement_status"),
                    "constraint_results": decision.get("constraint_results", {}),
                    "safety_review": decision.get("safety_review"),
                    "route_contract": decision.get("route_contract"),
                    "candidate_evaluations": [],
                })
                response_type = (
                    "weather_route_replanned"
                    if decision.get("action_type") == "weather_safe_route_replan"
                    else "weather_speed_restricted"
                )
                self.record(response_type, {
                    "tick": event_tick, "task_id": decision.get("task_id"),
                    "vehicle_id": decision.get("vehicle_id"),
                    "road_id": degraded_edge_id,
                    "estimated_delay_s": decision.get("estimated_delay_s"),
                    "status": "planned",
                })
                record_count += 2
            for task in result.get("tasks", []):
                if not isinstance(task, dict) or not task.get("task_id"):
                    continue
                self.record("task_completed", {
                    "tick": task.get("completed_tick"),
                    "task_id": task.get("task_id"),
                    "status": task.get("status"),
                    "reason": task.get("status_reason"),
                })
                record_count += 1
            self.record("road_restored", {
                "tick": recovery_tick, "road_id": degraded_edge_id,
                "status": "OPEN",
            })
            self.record("weather_recovered", {
                "tick": recovery_tick, "status": "clear",
            })
            self.record("run_completed", {
                "tick": recovery_tick, "status": result.get("status"),
                "mode": result.get("mode"),
            })
            return record_count + 3

        closed_edge_id = result.get("closed_edge_id")
        if closed_edge_id:
            record_count = 0
            self.record(
                "road_closed",
                {
                    "tick": 30,
                    "road_id": closed_edge_id,
                    "status": "CLOSED",
                    "source": "structural_scenario_result",
                },
            )
            record_count += 1
            for route_plan in result.get("route_plans", []):
                if isinstance(route_plan, dict):
                    self._store(
                        lambda store, item=route_plan: store.record_route_plan(
                            self.run_id, item
                        )
                    )
            for change in result.get("route_changes", []):
                if not isinstance(change, dict):
                    continue
                self.record(
                    "agent_decision",
                    {
                        "decision_id": "{}:road-replan".format(change.get("task_id")),
                        "context": "road_closure_route_replan",
                        "task_id": change.get("task_id"),
                        "scheduler_agent_action": {
                            "action_type": change.get("action_type"),
                            "assigned_vehicle_id": change.get("vehicle_id"),
                            "policy_version": result.get("route_planner_version"),
                            "reason": "closed_edge_avoided",
                        },
                        "closed_edge_id": closed_edge_id,
                        "original_distance_m": change.get("original_distance_m"),
                        "replanned_distance_m": change.get("replanned_distance_m"),
                        "constraint_results": change.get("constraint_results", {}),
                        "safety_review": change.get("safety_review"),
                        "route_contract": change.get("route_contract"),
                        "candidate_evaluations": [],
                    },
                )
                self.record(
                    "route_replanned",
                    {
                        "tick": 30,
                        "task_id": change.get("task_id"),
                        "vehicle_id": change.get("vehicle_id"),
                        "closed_edge_id": closed_edge_id,
                        "action_type": change.get("action_type"),
                        "status": "planned",
                        "reason": "road_closure",
                    },
                )
                record_count += 2
                if change.get("takeover_required"):
                    self.record(
                        "task_reassigned",
                        {
                            "tick": 30,
                            "task_id": change.get("task_id"),
                            "original_vehicle_id": change.get("original_vehicle_id"),
                            "vehicle_id": change.get("vehicle_id"),
                            "status": "assigned",
                            "reason": "original_vehicle_has_no_safe_road_bypass",
                        },
                    )
                    record_count += 1
            for task in result.get("tasks", []):
                if not isinstance(task, dict) or not task.get("task_id"):
                    continue
                self.record(
                    "task_completed",
                    {
                        "tick": task.get("completed_tick"),
                        "task_id": task.get("task_id"),
                        "status": task.get("status"),
                        "reason": task.get("status_reason"),
                    },
                )
                record_count += 1
            self.record(
                "road_reopened",
                {"tick": 31, "road_id": closed_edge_id, "status": "OPEN"},
            )
            self.record(
                "run_completed",
                {"tick": 31, "status": result.get("status"), "mode": result.get("mode")},
            )
            return record_count + 2

        failed_vehicle_id = result.get("failed_vehicle_id")
        if not failed_vehicle_id:
            return 0
        tick = result.get("failure_tick")
        record_count = 0
        self.record(
            "vehicle_fault",
            {
                "tick": tick,
                "vehicle_id": failed_vehicle_id,
                "status": "fault",
                "source": "structural_scenario_result",
            },
        )
        record_count += 1
        for task_id in result.get("released_task_ids", []):
            self.record(
                "task_released",
                {
                    "tick": tick,
                    "task_id": task_id,
                    "reason": "released_after_vehicle_fault",
                },
            )
            record_count += 1
        for assignment in result.get("assignments", []):
            if not isinstance(assignment, dict):
                continue
            self.record(
                "task_reassigned",
                {
                    "tick": tick,
                    "task_id": assignment.get("task_id"),
                    "vehicle_id": assignment.get("vehicle_id"),
                    "reason": "vehicle_failure_takeover",
                    "score": assignment.get("score"),
                },
            )
            record_count += 1
        for task in result.get("tasks", []):
            if not isinstance(task, dict) or not task.get("task_id"):
                continue
            self.record(
                "task_completed",
                {
                    "tick": task.get("completed_tick"),
                    "task_id": task.get("task_id"),
                    "status": task.get("status"),
                    "reason": task.get("status_reason"),
                },
            )
            record_count += 1
        self.record(
            "run_completed",
            {
                "tick": None,
                "status": result.get("status"),
                "mode": result.get("mode"),
            },
        )
        return record_count + 1

    def write_json(self, name: str, payload: Dict[str, Any]) -> Path:
        path = self.run_dir / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._store(
            lambda store: store.record_artifact(
                self.run_id, path.stem, path
            )
        )
        if name == "summary.json":
            self._store(
                lambda store: store.update_from_summary(self.run_id, payload)
            )
        return path

    def write_jsonl(
        self, name: str, items: Iterable[Dict[str, Any]]
    ) -> Path:
        path = self.run_dir / name
        record_count = 0
        with path.open("w", encoding="utf-8") as handle:
            for item in items:
                handle.write(
                    json.dumps(
                        item, ensure_ascii=False, sort_keys=True
                    )
                    + "\n"
                )
                record_count += 1
        self._store(
            lambda store: store.record_artifact(
                self.run_id, path.stem, path, record_count
            )
        )
        return path

    def append_jsonl(
        self, name: str, items: Iterable[Dict[str, Any]]
    ) -> Path:
        path = self.run_dir / name
        record_count = 0
        with path.open("a", encoding="utf-8") as handle:
            for item in items:
                handle.write(
                    json.dumps(
                        item, ensure_ascii=False, sort_keys=True
                    )
                    + "\n"
                )
                record_count += 1
        self._store(
            lambda store: store.record_artifact(
                self.run_id, path.stem, path, record_count
            )
        )
        return path

    def close(self) -> None:
        """Close the optional sidecar store after the run has finished."""
        if self.store is not None:
            try:
                self.store.finalize_unfinished_run(self.run_id)
            finally:
                self.store.close()
