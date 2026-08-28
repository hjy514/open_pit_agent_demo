#!/usr/bin/env python3
"""Print a concise Chinese acceptance result for the latest completed run."""

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS = PROJECT_ROOT / "artifacts" / "runs"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _latest_completed(
    root: Path, scenario_id: str, mode: str
) -> Path:
    candidates = []
    if root.exists():
        for item in root.iterdir():
            if not item.is_dir():
                continue
            summary = _read_json(item / "summary.json")
            report = _read_json(item / "acceptance_report.json")
            if (
                summary
                and report
                and (
                    not scenario_id
                    or summary.get("scenario_id") == scenario_id
                )
                and (not mode or summary.get("mode") == mode)
            ):
                candidates.append(item)
    if not candidates:
        raise RuntimeError("没有找到带验收报告的已完成运行")
    return max(candidates, key=lambda item: item.stat().st_mtime)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS)
    parser.add_argument(
        "--scenario-id",
        default="town03-competition-demo-v1",
    )
    parser.add_argument("--mode", default="carla-run")
    args = parser.parse_args()
    try:
        run_dir = (
            args.run_dir.expanduser().resolve()
            if args.run_dir
            else _latest_completed(
                args.runs_root.expanduser().resolve(),
                args.scenario_id,
                args.mode,
            )
        )
    except RuntimeError as exc:
        print("检查失败：{}".format(exc))
        print("请先在窗口中完成“比赛一键综合演示（推荐）”。")
        return 2
    summary = _read_json(run_dir / "summary.json")
    report = _read_json(run_dir / "acceptance_report.json")
    if not summary or not report:
        print("检查失败：运行目录缺少 summary.json 或 acceptance_report.json")
        print("目录：{}".format(run_dir))
        return 2

    print("运行结果：{}".format(report.get("overall_status", "UNKNOWN")))
    print(
        "功能得分：{}/{}（{:.0%}）".format(
            report.get("passed_checks", 0),
            report.get("required_checks", 0),
            float(report.get("functional_score", 0.0)),
        )
    )
    print(
        "任务完成：{}/{}".format(
            sum(
                item.get("status") == "completed"
                for item in summary.get("tasks", [])
            ),
            len(summary.get("tasks", [])),
        )
    )
    print(
        "风险等级序列：{}".format(
            " → ".join(
                item.get("level", "?")
                for item in summary.get("risk_assessments", [])
            )
            or "无"
        )
    )
    intelligence = summary.get("decision_intelligence", {})
    memory = summary.get("imitation_memory", {})
    print(
        "协同决策记录：{} 条；经验样本：{} 条；已训练模型：{}".format(
            intelligence.get("decision_record_count", 0),
            intelligence.get("experience_count", 0),
            "是" if intelligence.get("model_trained") else "否",
        )
    )
    print(
        "历史模仿记忆：{} 条成功经验，{} 项有界偏好".format(
            memory.get("successful_experience_count", 0),
            memory.get("preference_count", 0),
        )
    )
    if summary.get("monitoring_layout_id"):
        print(
            "监测数据：{}个固定站，{}辆移动设备；"
            "固定观测{}条，移动观测{}条".format(
                summary.get("fixed_station_count", 0),
                summary.get("mobile_equipment_count", 0),
                summary.get("fixed_observation_count", 0),
                summary.get("mobile_observation_count", 0),
            )
        )
    if summary.get("closed_loop_feedback_count"):
        print(
            "闭环反馈：{}条复核结果；决策{}；闭环{}".format(
                summary.get("closed_loop_feedback_count", 0),
                json.dumps(
                    summary.get("closed_loop_decision_counts", {}),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                (
                    "完成"
                    if summary.get("monitoring_dispatch_closed_loop")
                    else "未完成"
                ),
            )
        )
    failed = [
        item
        for item in report.get("checks", [])
        if item.get("required") and not item.get("passed")
    ]
    if failed:
        print("未通过项：")
        for item in failed:
            print(
                "- {}：{}".format(
                    item.get("check_id"),
                    json.dumps(
                        item.get("evidence"),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
            )
    else:
        print("所有适用的功能检查均已通过。")
    print("证据目录：{}".format(run_dir))
    scenario_id = str(
        report.get("scenario_id") or summary.get("scenario_id") or ""
    )
    if scenario_id.startswith("openpit-mine"):
        scope = (
            "CARLA 0325_5露天矿仿真地图下的功能闭环验收，"
            "不是矿山工业安全认证"
        )
    else:
        scope = report.get(
            "scope",
            "功能闭环验收，不是矿山工业安全认证",
        )
    print("说明：{}。".format(scope))
    return 0 if report.get("overall_status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
