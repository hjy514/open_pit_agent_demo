#!/usr/bin/env python3
"""Unified entry for existing CARLA-free structural baseline scenarios."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from open_pit_agent.config import load_config
from open_pit_agent.decision_intelligence import (
    aggregate_structural_transition_datasets, load_structural_reward_config,
    write_structural_transition_dataset, export_closed_loop_transition_dataset,
)
from open_pit_agent.decision import (
    train_and_save_behavior_cloning_policy,
    train_and_save_global_behavior_cloning_policy,
)
from open_pit_agent.evidence import EvidenceRecorder
from open_pit_agent.sqlite_store import SqliteRunStore
from open_pit_agent.adapters.carla_adapter import CarlaAdapterError
from open_pit_agent.scenario import (
    SCENARIO_CATALOG, SUPPORTED_STRUCTURAL_SCENARIOS, run_structural_scenario,
    summarize_structural_batch, run_carla_scenario_execution,
    normalize_scenario_run_result, compatibility_config_path,
    validate_scenario_request,
)


DEFAULT_CONFIGS = {
    key: compatibility_config_path(key, SCENARIO_CATALOG)
    for key in SCENARIO_CATALOG if key != "s08"
}
DECISION_CONFIG = ROOT / "configs" / "dispatch_cost_v1.json"


def _summary_for_storage(result, config_path, random_map):
    """Build a database summary without claiming CARLA physical execution."""
    summary = dict(result)
    summary["scenario_seed"] = result.get("seed")
    summary["scenario_mode"] = "seeded_random_map" if random_map else "fixed_structural"
    summary["mode"] = result.get("mode")
    summary["config_path"] = str(config_path)
    policy = result.get("policy_comparison", {})
    if isinstance(policy, dict) and policy:
        summary["policy_version"] = policy.get("executed_policy")
        summary["shadow_policy_version"] = policy.get("shadow_policy")
        summary["optimizer_version"] = policy.get("optimizer_version")
    if isinstance(result.get("tasks"), list):
        summary["tasks"] = result["tasks"]
        return summary
    task_drafts = {
        str(item.get("task_id")): item
        for item in result.get("map_resource_task_draft", [])
        if isinstance(item, dict) and item.get("task_id")
    }
    summary["tasks"] = [
        {
            "task_id": item.get("task_id"),
            "zone_id": item.get("zone_id") or item.get("task_id"),
            "task_type": task_drafts.get(str(item.get("task_id")), {}).get("task_type"),
            "status": "completed",
            "assigned_vehicle_id": item.get("vehicle_id"),
            "score": item.get("score"),
            "status_reason": "structural_mock_completion",
        }
        for item in result.get("assignments", [])
        if isinstance(item, dict)
    ]
    return summary


def _record_result(result, config_path, random_map):
    recorder = EvidenceRecorder(ROOT / "artifacts" / "runs", result["scenario_id"])
    try:
        result["run_id"] = recorder.run_id
        result["run_directory"] = str(recorder.run_dir)
        result["database_path"] = str(recorder.database_path)
        result["database_recording"] = recorder.store is not None
        result["policy_comparison_decision_record_count"] = (
            recorder.record_policy_comparison(result)
        )
        result["execution_feedback_record_count"] = (
            recorder.record_execution_feedback(result)
        )
        result["closed_loop_cycle_record_count"] = (
            recorder.record_closed_loop_cycle(result)
        )
        if result.get("mode") in {
            "carla_multi_vehicle_execution", "carla_multi_scenario_execution"
        }:
            result["runtime_event_record_count"] = (
                recorder.record_runtime_events(result)
            )
            result["structural_event_record_count"] = 0
        else:
            result["structural_event_record_count"] = (
                recorder.record_structural_events(result)
            )
        result["decision_record_count"] = (
            recorder.store.connection.execute(
                "SELECT count(*) FROM decisions WHERE run_id=?", (recorder.run_id,)
            ).fetchone()[0]
            if recorder.store is not None else None
        )
        summary = _summary_for_storage(result, config_path, random_map)
        summary_path = recorder.write_json("summary.json", summary)
        if recorder.store is None:
            database_validation = {
                "status": "NOT_AVAILABLE",
                "reason": "SQLite evidence sidecar unavailable",
            }
        else:
            database_validation = recorder.store.validate_closed_loop_evidence(
                recorder.run_id, result.get("scenario_key"), result
            )
        result["database_evidence_validation"] = database_validation
        result["closed_loop_status"] = (
            "CLOSED_LOOP_PASS"
            if result.get("closed_loop_validation", {}).get("status") == "CLOSED_LOOP_PASS"
            and database_validation.get("status") == "EVIDENCE_PASS"
            else "NOT_AVAILABLE" if database_validation.get("status") == "NOT_AVAILABLE"
            else "FAILED"
        )
        result = normalize_scenario_run_result(
            result,
            scenario_key=str(result.get("scenario_key") or "unknown"),
            map_context=result.get("map_context") or result.get(
                "provenance", {}
            ).get("map_context", {}),
        )
        summary = _summary_for_storage(result, config_path, random_map)
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if recorder.store is not None:
            recorder.store.update_from_summary(recorder.run_id, summary)
    finally:
        recorder.close()
    return result


def _run_one(scenario, config_path, seed, random_map, vehicle_count, no_record,
             execution_policy):
    result = run_structural_scenario(
        scenario, load_config(config_path), seed,
        random_map=random_map, vehicle_count=vehicle_count,
        execution_policy=execution_policy,
    )
    if no_record:
        result["database_evidence_validation"] = {
            "status": "NOT_AVAILABLE", "reason": "--no-record"
        }
        return result
    return _record_result(result, config_path, random_map)


def _record_failed_batch_run(scenario, config_path, seed, error):
    config = load_config(config_path)
    result = {
        "status": "FAIL", "mode": "mock_structural",
        "scenario_id": config.scenario_id, "scenario_key": scenario,
        "seed": seed, "task_count": 0, "completed_task_count": 0,
        "error": "{}: {}".format(type(error).__name__, error),
        "closed_loop_status": "FAILED",
    }
    recorder = EvidenceRecorder(ROOT / "artifacts" / "runs", config.scenario_id)
    try:
        result["run_id"] = recorder.run_id
        result["run_directory"] = str(recorder.run_dir)
        result["database_path"] = str(recorder.database_path)
        recorder.record("run_failed", {"seed": seed, "error": result["error"]})
        recorder.write_json(
            "summary.json", _summary_for_storage(result, config_path, True)
        )
    finally:
        recorder.close()
    return result


def summarize_policy_ab_pairs(results, expected_pair_count):
    """Summarize actual same-scenario, same-seed policy executions."""
    grouped = {}
    for item in results:
        key = (str(item.get("scenario_key")), item.get("seed"))
        policy = item.get("resolved_execution_policy")
        if policy in {"heuristic", "multi-objective"}:
            grouped.setdefault(key, {})[policy] = item

    pairs = []
    for (scenario_key, seed), policies in sorted(grouped.items()):
        heuristic = policies.get("heuristic")
        optimized = policies.get("multi-objective")
        if heuristic is None or optimized is None:
            pairs.append({
                "scenario_key": scenario_key,
                "seed": seed,
                "status": "INCOMPLETE_PAIR",
                "available_policies": sorted(policies),
            })
            continue
        heuristic_assignments = {
            str(item.get("task_id")): str(item.get("vehicle_id"))
            for item in heuristic.get("assignments", [])
            if isinstance(item, dict) and item.get("task_id")
        }
        optimized_assignments = {
            str(item.get("task_id")): str(item.get("vehicle_id"))
            for item in optimized.get("assignments", [])
            if isinstance(item, dict) and item.get("task_id")
        }
        task_ids = set(heuristic_assignments) | set(optimized_assignments)
        changed = sum(
            heuristic_assignments.get(task_id)
            != optimized_assignments.get(task_id)
            for task_id in task_ids
        )
        metrics = optimized.get("policy_comparison", {}).get("ab_metrics", {})
        both_pass = all(
            item.get("status") == "PASS" for item in (heuristic, optimized)
        )
        both_closed_loop = all(
            item.get("closed_loop_status") == "CLOSED_LOOP_PASS"
            for item in (heuristic, optimized)
        )
        both_safety_pass = all(
            item.get("safety_shield", {}).get("status") == "PASS"
            for item in (heuristic, optimized)
        )
        pairs.append({
            "scenario_key": scenario_key,
            "seed": seed,
            "status": (
                "PAIR_PASS" if both_pass and both_closed_loop
                and both_safety_pass else "PAIR_FAIL"
            ),
            "heuristic_run_id": heuristic.get("run_id"),
            "multi_objective_run_id": optimized.get("run_id"),
            "both_runs_passed": both_pass,
            "both_closed_loops_passed": both_closed_loop,
            "both_safety_gates_passed": both_safety_pass,
            "assignment_changed": changed > 0,
            "changed_assignment_count": changed,
            "heuristic_normalized_cost": metrics.get(
                "heuristic_normalized_cost"
            ),
            "multi_objective_normalized_cost": metrics.get(
                "multi_objective_normalized_cost"
            ),
            "normalized_cost_improvement": metrics.get(
                "normalized_cost_improvement"
            ),
            "heuristic_route_length_m": metrics.get(
                "heuristic_route_length_m"
            ),
            "multi_objective_route_length_m": metrics.get(
                "multi_objective_route_length_m"
            ),
            "route_length_change_m": metrics.get("route_length_change_m"),
        })

    complete_pairs = [item for item in pairs if item["status"] != "INCOMPLETE_PAIR"]

    def average(field):
        values = [
            float(item[field]) for item in complete_pairs
            if item.get(field) is not None
        ]
        return round(sum(values) / len(values), 6) if values else None

    passed_pairs = sum(item["status"] == "PAIR_PASS" for item in complete_pairs)
    return {
        "status": (
            "A_B_PASS" if len(complete_pairs) == expected_pair_count
            and passed_pairs == expected_pair_count else "A_B_FAIL"
        ),
        "comparison_scope": "same_scenario_same_seed_structural_execution",
        "expected_pair_count": expected_pair_count,
        "complete_pair_count": len(complete_pairs),
        "passed_pair_count": passed_pairs,
        "assignment_changed_seed_count": sum(
            bool(item.get("assignment_changed")) for item in complete_pairs
        ),
        "changed_assignment_count": sum(
            int(item.get("changed_assignment_count") or 0)
            for item in complete_pairs
        ),
        "mean_normalized_cost_improvement": average(
            "normalized_cost_improvement"
        ),
        "mean_route_length_change_m": average("route_length_change_m"),
        "measurement_boundary": (
            "STRUCTURAL_ONLY_NO_PHYSICS; no CARLA travel-time, collision, "
            "energy or real-mine performance claim"
        ),
        "pairs": pairs,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SUPPORTED_STRUCTURAL_SCENARIOS + ("all",))
    parser.add_argument("--mode", choices=("structural", "carla"),
                        default="structural")
    parser.add_argument("--list-scenarios", action="store_true",
                        help="Print implemented and planned scenario status")
    parser.add_argument("--aggregate-datasets", action="store_true",
                        help="Aggregate quality-passed batches without running a scenario")
    parser.add_argument("--export-cycle-dataset", action="store_true",
                        help="Export direct V3 closed-loop DB cycles without running a scenario")
    parser.add_argument("--database-health", action="store_true",
                        help="Report openpit.db lifecycle and data volume without running a scenario")
    parser.add_argument("--repair-stale-runs", action="store_true",
                        help="Mark old running/planned rows INCOMPLETE; never deletes evidence")
    parser.add_argument("--stale-hours", type=float, default=24.0,
                        help="Age threshold for database health/repair (default: 24)")
    parser.add_argument("--train-bc", action="store_true",
                        help="Train the offline shadow BC candidate ranker")
    parser.add_argument("--dataset-version",
                        help="Immutable name used with dataset export/aggregation")
    parser.add_argument(
        "--database", type=Path, default=ROOT / "data" / "database" / "openpit.db",
        help="openpit.db used with --export-cycle-dataset",
    )
    parser.add_argument("--split-seed", type=int, default=202616)
    parser.add_argument("--model-version",
                        help="Immutable model version used with --train-bc")
    parser.add_argument("--policy-scope", choices=("candidate", "global"),
                        default="candidate", help="BC decision scope")
    parser.add_argument("--epochs", type=int, default=400)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--random-map", action="store_true",
                        help="S01/S02: use a seeded global P5 map-resource workload")
    parser.add_argument("--vehicle-count", type=int, default=6,
                        help="Fleet size admitted by Scenario Catalog (currently 6 or 8)")
    parser.add_argument(
        "--policy", choices=("heuristic", "multi-objective", "auto"),
        default="heuristic",
        help=("Executed policy; auto uses multi-objective for random S01/S02 "
              "and each event scenario's native safe policy"),
    )
    parser.add_argument(
        "--compare-policies", action="store_true",
        help=("Run Heuristic V0 and MultiObjective V1 on identical random "
              "S01/S02 seeds and write one paired A/B summary"),
    )
    parser.add_argument("--runs", type=int, default=1,
                        help="Number of consecutive seeds per selected scenario")
    parser.add_argument("--seed-step", type=int, default=1)
    parser.add_argument("--no-record", action="store_true",
                        help="Do not write this structural run to artifacts/openpit.db")
    parser.add_argument("--ticks", type=int, default=1000,
                        help="Maximum CARLA ticks for S01 physical execution")
    parser.add_argument("--load-map", action="store_true",
                        help="Ask CARLA to load the configured map")
    parser.add_argument("--check-only", action="store_true",
                        help="Check S01 CARLA connection and workload without spawning")
    args = parser.parse_args()
    if args.list_scenarios:
        print(json.dumps(SCENARIO_CATALOG, ensure_ascii=False, indent=2,
                         sort_keys=True))
        return 0
    if args.database_health or args.repair_stale_runs:
        if args.scenario or args.aggregate_datasets or args.train_bc \
                or args.export_cycle_dataset:
            parser.error("database health/repair cannot be combined with scenario/data actions")
        if not args.database.is_file():
            parser.error("database does not exist: {}".format(args.database))
        store = SqliteRunStore(args.database)
        try:
            repaired = (
                store.mark_stale_runs_incomplete(args.stale_hours)
                if args.repair_stale_runs else []
            )
            report = store.database_health_report(args.stale_hours)
        finally:
            store.close()
        report["repair"] = {
            "requested": bool(args.repair_stale_runs),
            "updated_run_count": len(repaired),
            "updated_run_ids": repaired,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.export_cycle_dataset:
        if args.aggregate_datasets or args.train_bc:
            parser.error("--export-cycle-dataset cannot be combined with aggregation/training")
        if not args.dataset_version:
            parser.error("--dataset-version is required with --export-cycle-dataset")
        scenario_filter = None
        if args.scenario and args.scenario != "all":
            scenario_filter = [args.scenario]
        manifest = export_closed_loop_transition_dataset(
            args.database, ROOT / "data" / "datasets", args.dataset_version,
            scenario_keys=scenario_filter,
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if manifest["dataset_quality"]["status"] == "DATASET_QUALITY_PASS" else 1
    reward_config = load_structural_reward_config(DECISION_CONFIG)
    if args.train_bc:
        if args.scenario or args.aggregate_datasets:
            parser.error("--train-bc cannot be combined with scenario execution or aggregation")
        if not args.dataset_version or not args.model_version:
            parser.error("--dataset-version and --model-version are required with --train-bc")
        if args.epochs < 1:
            parser.error("--epochs must be at least 1")
        dataset_manifest = (
            ROOT / "data" / "datasets" / "openpit-structural-transition-v1"
            / "versions" / args.dataset_version / "manifest.json"
        )
        trainer = (
            train_and_save_global_behavior_cloning_policy
            if args.policy_scope == "global"
            else train_and_save_behavior_cloning_policy
        )
        report = trainer(dataset_manifest, ROOT / "data" / "models",
                         args.model_version, epochs=args.epochs)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.aggregate_datasets:
        if args.scenario:
            parser.error("--aggregate-datasets cannot be combined with --scenario")
        if not args.dataset_version:
            parser.error("--dataset-version is required with --aggregate-datasets")
        manifest = aggregate_structural_transition_datasets(
            ROOT / "data" / "datasets", args.dataset_version,
            split_seed=args.split_seed,
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if manifest["status"].startswith("DATASET_VERSION_READY") else 1
    if not args.scenario:
        parser.error("--scenario is required unless --aggregate-datasets is used")
    if args.scenario != "all":
        try:
            validate_scenario_request(
                args.scenario, args.mode, args.vehicle_count, SCENARIO_CATALOG
            )
        except ValueError as exc:
            parser.error(str(exc))
    if args.mode == "carla":
        if args.runs != 1:
            parser.error("CARLA execution currently requires --runs 1")
        if args.scenario not in DEFAULT_CONFIGS:
            parser.error("CARLA execution supports S01-S07/S09; S08 uses run_scenario.sh")
        config_path = args.config or DEFAULT_CONFIGS[args.scenario]
        try:
            result = run_carla_scenario_execution(
                args.scenario, load_config(config_path), seed=args.seed,
                vehicle_count=args.vehicle_count, ticks=args.ticks,
                load_map=args.load_map, check_only=args.check_only,
                execution_policy=args.policy,
            )
        except (CarlaAdapterError, ValueError, RuntimeError) as exc:
            print("ERROR: CARLA执行准备失败：{}".format(exc), file=sys.stderr)
            if isinstance(exc, CarlaAdapterError):
                print("请确认CARLA已启动且地图已完全加载。", file=sys.stderr)
            else:
                print("请按上述资源准入提示处理后重试。", file=sys.stderr)
            return 2
        if not args.check_only and not args.no_record:
            result = _record_result(result, config_path, True)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("status") in {"PASS", "READY"} else 1
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.config and args.scenario == "all":
        parser.error("--config cannot be used with --scenario all")
    if args.compare_policies:
        if args.scenario not in {"s01", "s02"}:
            parser.error("--compare-policies supports S01 or S02 only")
        if args.policy != "heuristic":
            parser.error("--compare-policies already runs both policies; omit --policy")

    scenarios = list(SUPPORTED_STRUCTURAL_SCENARIOS) if args.scenario == "all" else [args.scenario]
    if args.scenario == "all":
        for scenario in scenarios:
            try:
                validate_scenario_request(
                    scenario, args.mode, args.vehicle_count, SCENARIO_CATALOG
                )
            except ValueError as exc:
                parser.error(str(exc))
    batch_mode = args.scenario == "all" or args.runs > 1 or args.compare_policies
    results = []
    for scenario in scenarios:
        config_path = args.config or DEFAULT_CONFIGS[scenario]
        config = load_config(config_path)
        base_seed = config.demo.random_seed if args.seed is None else args.seed
        for run_index in range(args.runs):
            run_seed = int(base_seed) + run_index * int(args.seed_step)
            if args.compare_policies:
                policies = ("heuristic", "multi-objective")
            else:
                resolved_policy = args.policy
                if args.policy == "auto":
                    resolved_policy = (
                        "multi-objective" if scenario in {"s01", "s02"}
                        and (args.random_map or args.scenario == "all")
                        else "heuristic"
                    )
                policies = (resolved_policy,)
            for resolved_policy in policies:
                try:
                    item = _run_one(
                        scenario, config_path, run_seed,
                        args.random_map or args.scenario == "all"
                        or args.compare_policies,
                        args.vehicle_count, args.no_record, resolved_policy,
                    )
                except Exception as exc:
                    if not batch_mode:
                        raise
                    item = ({
                        "status": "FAIL", "scenario_key": scenario,
                        "scenario_id": config.scenario_id, "seed": run_seed,
                        "task_count": 0, "completed_task_count": 0,
                        "error": "{}: {}".format(type(exc).__name__, exc),
                    } if args.no_record else _record_failed_batch_run(
                        scenario, config_path, run_seed, exc
                    ))
                item["requested_policy_profile"] = (
                    "paired-a-b" if args.compare_policies else args.policy
                )
                item["resolved_execution_policy"] = resolved_policy
                results.append(item)

    if not batch_mode:
        result = results[0]
        if not args.no_record:
            result["training_dataset"] = write_structural_transition_dataset(
                results, ROOT / "data" / "datasets", result["run_id"],
                reward_config=reward_config,
                expected_scenarios=scenarios,
            )
            if result.get("closed_loop_cycle"):
                result["closed_loop_training_dataset"] = (
                    export_closed_loop_transition_dataset(
                        result["database_path"], ROOT / "data" / "datasets",
                        result["run_id"], run_ids=[result["run_id"]],
                    )
                )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("status") == "PASS" else 1

    summary = summarize_structural_batch(results)
    summary.update({
        "batch_id": "{}-{}-{}".format(
            "policy-ab" if args.compare_policies else "structural-batch",
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            uuid4().hex[:8],
        ),
        "vehicle_count": args.vehicle_count,
        "runs_per_scenario": args.runs,
        "seed_step": args.seed_step,
        "database_recording": not args.no_record,
        "requested_execution_policy": args.policy,
        "simulation_claim": "structural_mock_batch_only_no_carla_physics",
        "runs": [{
            "scenario_key": item.get("scenario_key"),
            "scenario_id": item.get("scenario_id"),
            "seed": item.get("seed"), "status": item.get("status"),
            "result_schema_version": item.get("result_schema_version"),
            "lifecycle_phase": item.get("lifecycle", {}).get("current_phase"),
            "run_id": item.get("run_id"), "error": item.get("error"),
            "resolved_execution_policy": item.get(
                "resolved_execution_policy"
            ),
            "closed_loop_status": item.get("closed_loop_status"),
            "safety_shield_status": item.get("safety_shield", {}).get(
                "status"
            ),
            "safety_review_count": item.get("safety_shield", {}).get(
                "review_count"
            ),
            "safety_rejected_count": item.get("safety_shield", {}).get(
                "rejected_count"
            ),
            "database_evidence_status": item.get(
                "database_evidence_validation", {}
            ).get("status"),
        } for item in results],
    })
    if args.compare_policies:
        summary["requested_execution_policy"] = "paired-a-b"
        summary["policy_ab_comparison"] = summarize_policy_ab_pairs(
            results, len(scenarios) * args.runs
        )
        if summary["policy_ab_comparison"]["status"] != "A_B_PASS":
            summary["status"] = "FAIL"
    summary["summary_path"] = None
    if not args.no_record:
        summary["training_dataset"] = write_structural_transition_dataset(
            results, ROOT / "data" / "datasets", summary["batch_id"],
            reward_config=reward_config,
            expected_scenarios=scenarios,
        )
        batch_run_ids = [
            str(item["run_id"]) for item in results
            if item.get("run_id") and item.get("closed_loop_cycle")
        ]
        if batch_run_ids:
            summary["closed_loop_training_dataset"] = (
                export_closed_loop_transition_dataset(
                ROOT / "data" / "database" / "openpit.db",
                ROOT / "data" / "datasets", summary["batch_id"],
                run_ids=batch_run_ids,
            )
            )
        else:
            summary["closed_loop_training_dataset"] = {
                "status": "NOT_AVAILABLE_NO_DIRECT_CYCLES",
                "record_count": 0,
            }
        batch_dir = ROOT / "artifacts" / "batches" / summary["batch_id"]
        batch_dir.mkdir(parents=True, exist_ok=False)
        summary_path = batch_dir / "summary.json"
        summary["summary_path"] = str(summary_path)
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
