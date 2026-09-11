"""Allowlisted local process control for demonstration scenarios."""

import json
import signal
import os
from random import SystemRandom
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .scenario.catalog import (
    compatibility_config_path,
    load_scenario_catalog,
    validate_scenario_request,
)
from .config import ConfigError, load_config
from .adapters.carla_adapter import CarlaAdapter, CarlaAdapterError


class ControlError(RuntimeError):
    """Raised when a requested control action is unsafe or invalid."""


@dataclass(frozen=True)
class DemoScenario:
    scenario_id: str
    display_name: str
    description: str
    config_path: Path
    ticks: int
    risk_config_path: Optional[Path] = None
    monitoring_config_path: Optional[Path] = None
    inject_failure: bool = False

    def public_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "display_name": self.display_name,
            "description": self.description,
            "ticks": self.ticks,
            "risk_enabled": self.risk_config_path is not None,
            "fixed_monitoring_enabled": (
                self.monitoring_config_path is not None
            ),
            "failure_enabled": self.inject_failure,
        }


def default_scenarios(project_root: Path) -> List[DemoScenario]:
    configs = Path(project_root) / "configs"
    return [
        DemoScenario(
            scenario_id="competition_demo",
            display_name="比赛一键综合演示（推荐）",
            description=(
                "自然语言任务、故障接管、风险升级、"
                "道路管控、应急响应和工单闭环"
            ),
            config_path=configs
            / "town03_competition_demo.json",
            risk_config_path=configs
            / "risk_slope_competition_synthetic.json",
            ticks=6000,
            inject_failure=True,
        ),
        DemoScenario(
            scenario_id="mine_competition_demo",
            display_name="露天矿边坡风险智能体装备集群演示",
            description=(
                "露天矿边坡巡检、装备故障接管、"
                "风险升级、运输道路管控和应急响应"
            ),
            config_path=configs
            / "mine_competition_demo.json",
            risk_config_path=configs
            / "risk_slope_competition_synthetic.json",
            monitoring_config_path=configs
            / "monitoring_demo.json",
            ticks=6000,
            inject_failure=False,
        ),
        DemoScenario(
            scenario_id="normal_inspection",
            display_name="正常三车巡检",
            description="验证三车能力分工、导航与到达闭环",
            config_path=configs / "town03.json",
            ticks=800,
        ),
        DemoScenario(
            scenario_id="long_patrol",
            display_name="长程连续巡检（推荐观察）",
            description=(
                "三车各执行两段长路线，镜头按任务优先级"
                "自动切换"
            ),
            config_path=configs
            / "town03_long_patrol.json",
            ticks=16000,
        ),
        DemoScenario(
            scenario_id="fault_recovery",
            display_name="故障抢占与恢复",
            description="注入车辆故障，验证任务接管和原任务恢复",
            config_path=configs
            / "town03_fault_recovery.json",
            ticks=1800,
            inject_failure=True,
        ),
        DemoScenario(
            scenario_id="orange_risk",
            display_name="橙色风险复核",
            description="合成风险升级并触发融合巡检车复核",
            config_path=configs
            / "town03_risk_response.json",
            risk_config_path=configs
            / "risk_slope_synthetic.json",
            ticks=1400,
        ),
        DemoScenario(
            scenario_id="red_response",
            display_name="红色风险多装备联动",
            description="道路管控、应急响应、复核和多工单闭环",
            config_path=configs
            / "town03_red_response.json",
            risk_config_path=configs
            / "risk_slope_red_synthetic.json",
            ticks=3000,
        ),
    ]


class DemoControlManager:
    def __init__(
        self,
        project_root: Path,
        artifacts_root: Path,
        scenarios: Optional[List[DemoScenario]] = None,
        python_executable: Optional[str] = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.artifacts_root = Path(artifacts_root).resolve()
        scenario_values = scenarios or default_scenarios(
            self.project_root
        )
        self._scenarios = {
            item.scenario_id: item
            for item in scenario_values
        }
        self.python_executable = (
            python_executable or sys.executable
        )
        self._process = None
        self._log_handle = None
        self._log_path = None
        self._active_scenario_id = None
        self._started_at = None
        self._stop_requested = False
        self._last_exit_code = None
        self._lock = threading.Lock()

    def scenarios(self) -> List[Dict[str, Any]]:
        return [
            self._scenarios[key].public_dict()
            for key in self._scenarios
        ]

    def command_for(self, scenario_id: str) -> List[str]:
        scenario = self._scenario(scenario_id)
        command = [
            self.python_executable,
            str(self.project_root / "run_demo.py"),
            "--mode",
            "carla-run",
            "--config",
            str(scenario.config_path),
            "--artifacts",
            str(self.artifacts_root),
            "--load-map",
            "--spawn-missing",
            "--ticks",
            str(scenario.ticks),
        ]
        if scenario.risk_config_path is not None:
            command.extend(
                [
                    "--risk-config",
                    str(scenario.risk_config_path),
                ]
            )
        if scenario.monitoring_config_path is not None:
            command.extend(
                [
                    "--monitoring-config",
                    str(scenario.monitoring_config_path),
                ]
            )
        if scenario.inject_failure:
            command.append("--inject-failure")
        return command

    def start(self, scenario_id: str) -> Dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            if (
                self._process is not None
                and self._process.poll() is None
            ):
                raise ControlError(
                    "A demo is already running: {}".format(
                        self._active_scenario_id
                    )
                )
            scenario = self._scenario(scenario_id)
            self._validate_scenario_files(scenario)
            log_root = self.artifacts_root.parent / "control"
            log_root.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(
                timezone.utc
            ).strftime("%Y%m%dT%H%M%SZ")
            self._log_path = log_root / "{}-{}.log".format(
                timestamp, scenario.scenario_id
            )
            self._log_handle = self._log_path.open(
                "a", encoding="utf-8"
            )
            self._process = subprocess.Popen(
                self.command_for(scenario_id),
                cwd=str(self.project_root),
                stdin=subprocess.DEVNULL,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self._active_scenario_id = scenario_id
            self._started_at = datetime.now(
                timezone.utc
            ).isoformat()
            self._stop_requested = False
            self._last_exit_code = None
            return self._status_locked()

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            if (
                self._process is None
                or self._process.poll() is not None
            ):
                raise ControlError(
                    "No demo process is currently running"
                )
            self._process.send_signal(signal.SIGINT)
            self._stop_requested = True
            return self._status_locked()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            return self._status_locked()

    def close(self) -> None:
        with self._lock:
            self._refresh_locked()
            if (
                self._process is not None
                and self._process.poll() is None
            ):
                self._process.send_signal(signal.SIGINT)
                self._stop_requested = True

    def _scenario(self, scenario_id: str) -> DemoScenario:
        try:
            return self._scenarios[str(scenario_id)]
        except KeyError as exc:
            raise ControlError(
                "Unknown allowlisted scenario: {}".format(
                    scenario_id
                )
            ) from exc

    @staticmethod
    def _validate_scenario_files(
        scenario: DemoScenario,
    ) -> None:
        required = [scenario.config_path]
        if scenario.risk_config_path is not None:
            required.append(scenario.risk_config_path)
        if scenario.monitoring_config_path is not None:
            required.append(scenario.monitoring_config_path)
        missing = [
            str(path) for path in required if not path.is_file()
        ]
        if missing:
            raise ControlError(
                "Scenario files are missing: {}".format(
                    ", ".join(missing)
                )
            )

    def _refresh_locked(self) -> None:
        if self._process is None:
            return
        exit_code = self._process.poll()
        if exit_code is None:
            return
        self._last_exit_code = exit_code
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def _status_locked(self) -> Dict[str, Any]:
        running = (
            self._process is not None
            and self._process.poll() is None
        )
        if running and self._stop_requested:
            state = "stopping"
        elif running:
            state = "running"
        elif self._active_scenario_id is not None:
            state = "finished"
        else:
            state = "idle"
        return {
            "state": state,
            "running": running,
            "scenario_id": self._active_scenario_id,
            "pid": (
                self._process.pid
                if running and self._process is not None
                else None
            ),
            "started_at": self._started_at,
            "stop_requested": self._stop_requested,
            "exit_code": self._last_exit_code,
            "log_path": (
                str(self._log_path)
                if self._log_path is not None
                else None
            ),
            "log_tail": self._read_log_tail(),
        }

    def _read_log_tail(self, limit: int = 30) -> List[str]:
        if self._log_path is None or not self._log_path.exists():
            return []
        try:
            return self._log_path.read_text(
                encoding="utf-8"
            ).splitlines()[-limit:]
        except OSError:
            return []


class UnifiedScenarioControlManager:
    """Allowlisted process control for the common S01-S09 entry point."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.catalog = load_scenario_catalog()
        self._process = None
        self._log_handle = None
        self._log_path = None
        self._request = None
        self._started_at = None
        self._stop_requested = False
        self._last_exit_code = None
        self._last_result = None
        self._pause_requested = False
        self._control_path = None
        self._lock = threading.Lock()
        self._carla_process = None
        self._carla_log_handle = None
        self._carla_log_path = None
        self._carla_state = "not_checked"
        self._carla_error = None
        self._scenario_error = None
        self._startup_cancel = threading.Event()

    def scenarios(self) -> List[Dict[str, Any]]:
        """Expose UI-safe metadata without absolute config paths."""
        result = []
        for key in sorted(self.catalog):
            item = self.catalog[key]
            result.append({
                "scenario_id": key,
                "display_name": item["name"],
                "description": item["description"],
                "scenario_type": item["type"],
                "modes": list(item["modes"]),
                "vehicle_counts": [
                    int(value) for value in
                    item["fleet"]["selectable_vehicle_counts"]
                ],
                "default_vehicle_count": int(
                    item["fleet"]["default_vehicle_count"]
                ),
                "policies": list(item["supported_policies"]),
                "carla_readiness": item["carla_readiness"],
                "implementation_mode": (
                    "legacy_golden_compatibility_adapter"
                    if key == "s08" else "unified_event_pipeline"
                ),
            })
        return result

    def command_for(self, request: Dict[str, Any]) -> List[str]:
        resolved = self._validate_request(request)
        command = [
            str(self.project_root / "run_scenario.sh"),
            "--scenario", resolved["scenario_id"],
            "--mode", resolved["mode"],
        ]
        # S08 is a fixed Golden compatibility implementation. Its public
        # request is unified, while unsupported random arguments must not leak
        # into the regression-tested launcher.
        if resolved["scenario_id"] != "s08":
            command.extend([
                "--vehicle-count", str(resolved["vehicle_count"]),
                "--seed", str(resolved["seed"]),
                "--policy", resolved["policy"],
                "--random-map",
            ])
            # Both modes publish the same RuntimeState contract.  Structural
            # mode replays logical phases; CARLA mode streams actual actor
            # telemetry and event controls.  Keeping this flag here means the
            # PyQt launcher never needs a separate execution path.
            if resolved["mode"] == "structural":
                command.extend([
                    "--ui-sync", "--playback-delay-seconds", "2.0",
                ])
            else:
                command.extend([
                    "--ui-sync",
                    "--display-speed-scale", "0.8",
                    "--operator-review",
                    "--operator-review-timeout-seconds", "120",
                    "--load-map",
                ])
        if resolved["check_only"]:
            command.append("--check-only")
        return command

    def start(self, request: Dict[str, Any]) -> Dict[str, Any]:
        resolved = self._validate_request(request)
        with self._lock:
            self._refresh_locked()
            if self._process is not None and self._process.poll() is None:
                raise ControlError("A scenario is already running: {}".format(
                    self._request["scenario_id"]
                ))
            self._request = resolved
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._stop_requested = False
            self._last_exit_code = None
            self._last_result = None
            self._pause_requested = False
            self._scenario_error = None
            self._startup_cancel.clear()
            if resolved["mode"] == "carla" and not self._carla_ready(resolved):
                self._launch_carla_locked(resolved)
                self._carla_state = "starting"
                watcher = threading.Thread(
                    target=self._wait_and_start_scenario,
                    args=(dict(resolved),), daemon=True,
                )
                watcher.start()
                return self._status_locked()
            self._carla_state = "ready" if resolved["mode"] == "carla" else "not_required"
            self._start_scenario_locked(resolved)
            return self._status_locked()

    def pause(self) -> Dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            if self._process is None or self._process.poll() is not None:
                raise ControlError("No scenario process is currently running")
            if self._request and self._request.get("scenario_id") == "s08":
                raise ControlError(
                    "S08当前使用Golden兼容执行器，尚不支持保留现场暂停"
                )
            self._write_control_state("paused")
            self._pause_requested = True
            return self._status_locked()

    def resume(self) -> Dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            if self._process is None or self._process.poll() is not None:
                raise ControlError("No scenario process is currently running")
            if not self._pause_requested:
                raise ControlError("Scenario is not paused")
            self._write_control_state("running")
            self._pause_requested = False
            return self._status_locked()

    def terminate(self) -> Dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            if self._process is None or self._process.poll() is not None:
                if self._carla_state == "starting":
                    self._startup_cancel.set()
                    self._stop_requested = True
                    return self._status_locked()
                raise ControlError("No scenario process is currently running")
            self._process.send_signal(signal.SIGINT)
            self._stop_requested = True
            self._pause_requested = False
            return self._status_locked()

    def stop(self) -> Dict[str, Any]:
        """Backward-compatible alias for callers that mean terminate."""
        return self.terminate()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            return self._status_locked()

    def close(self) -> None:
        with self._lock:
            self._refresh_locked()
            if self._process is not None and self._process.poll() is None:
                self._process.send_signal(signal.SIGINT)
                self._stop_requested = True

    def _validate_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(request, dict):
            raise ControlError("scenario request must be an object")
        scenario_id = str(request.get("scenario_id", "")).lower().strip()
        mode = str(request.get("mode", "structural")).lower().strip()
        if scenario_id not in self.catalog:
            raise ControlError("Unknown scenario: {}".format(scenario_id))
        entry = self.catalog[scenario_id]
        default_count = int(entry["fleet"]["default_vehicle_count"])
        random_seed = bool(request.get("random_seed", False))
        try:
            vehicle_count = int(request.get("vehicle_count", default_count))
            seed = int(request.get("seed", 202616))
        except (TypeError, ValueError) as exc:
            raise ControlError("vehicle_count and seed must be integers") from exc
        if seed < 0 or seed > 2147483647:
            raise ControlError("seed must be between 0 and 2147483647")
        # Random execution still resolves one concrete, persisted seed so the
        # exact task/route/event episode remains reproducible for evaluation.
        if random_seed:
            seed = SystemRandom().randint(0, 2147483647)
        try:
            validate_scenario_request(
                scenario_id, mode, vehicle_count, self.catalog
            )
        except ValueError as exc:
            raise ControlError(str(exc)) from exc
        policy = str(request.get("policy", "auto")).lower().strip()
        allowed_policies = set(entry["supported_policies"])
        if policy != "auto" and policy not in allowed_policies:
            raise ControlError(
                "scenario {} supports policies {}; requested={}".format(
                    scenario_id, sorted(allowed_policies), policy
                )
            )
        check_only = bool(request.get("check_only", False))
        if check_only and mode != "carla":
            raise ControlError("check_only is only valid in carla mode")
        return {
            "scenario_id": scenario_id,
            "mode": mode,
            "vehicle_count": vehicle_count,
            "seed": seed,
            "seed_mode": "random_generated" if random_seed else "fixed_replayable",
            "policy": policy,
            "check_only": check_only,
        }

    def _refresh_locked(self) -> None:
        if self._process is None:
            return
        exit_code = self._process.poll()
        if exit_code is None:
            return
        self._last_exit_code = exit_code
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        self._last_result = self._read_result_summary()

    def _carla_config(self, resolved: Dict[str, Any]):
        try:
            config_path = compatibility_config_path(
                resolved["scenario_id"], self.catalog
            )
            return load_config(config_path)
        except (ConfigError, OSError, ValueError) as exc:
            raise ControlError("无法读取CARLA场景配置：{}".format(exc))

    def _carla_ready(self, resolved: Dict[str, Any]) -> bool:
        config = self._carla_config(resolved)
        try:
            adapter = CarlaAdapter(config)
            adapter._import_carla()
            client = adapter.carla.Client(
                config.carla.host, int(config.carla.port)
            )
            client.set_timeout(min(2.0, config.carla.timeout_seconds))
            world = client.get_world()
            # A listening socket is not sufficient while UE is still loading.
            # Requiring both a parseable map and the project truck blueprint
            # prevents the scenario process from starting too early.
            world.get_map()
            if not world.get_blueprint_library().filter("vehicle.cat.cat"):
                self._carla_error = "CARLA已连接，但未找到vehicle.cat.cat蓝图"
                return False
            self._carla_error = None
            return True
        except (CarlaAdapterError, OSError, RuntimeError) as exc:
            self._carla_error = str(exc)
            return False

    def _launch_carla_locked(self, resolved: Dict[str, Any]) -> None:
        config = self._carla_config(resolved)
        launcher = config.carla.root / "CarlaUE4.sh"
        if not launcher.is_file() or not os.access(str(launcher), os.X_OK):
            raise ControlError("CARLA启动器不可用：{}".format(launcher))
        if self._carla_process is not None and self._carla_process.poll() is None:
            return
        log_root = self.project_root / "artifacts" / "control"
        log_root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._carla_log_path = log_root / "{}-carla.log".format(timestamp)
        self._carla_log_handle = self._carla_log_path.open("a", encoding="utf-8")
        # Deliberately no rendering, quality, OpenGL or off-screen flags.
        # The operator's verified command is exactly ./CarlaUE4.sh.
        self._carla_process = subprocess.Popen(
            [str(launcher)], cwd=str(config.carla.root),
            stdin=subprocess.DEVNULL, stdout=self._carla_log_handle,
            stderr=subprocess.STDOUT, start_new_session=True,
        )

    def _wait_and_start_scenario(self, resolved: Dict[str, Any]) -> None:
        deadline = time.time() + 90.0
        while time.time() < deadline and not self._startup_cancel.is_set():
            with self._lock:
                if self._carla_ready(resolved):
                    self._carla_state = "ready"
                    try:
                        self._start_scenario_locked(resolved)
                    except (OSError, ControlError) as exc:
                        self._scenario_error = str(exc)
                    return
                if self._carla_process is not None and self._carla_process.poll() is not None:
                    self._carla_state = "failed"
                    self._carla_error = "CARLA启动进程已退出"
                    return
            time.sleep(1.0)
        with self._lock:
            if self._startup_cancel.is_set():
                self._carla_state = "cancelled"
            else:
                self._carla_state = "timeout"
                self._carla_error = "等待CARLA RPC端口超时（90秒）"

    def _start_scenario_locked(self, resolved: Dict[str, Any]) -> None:
        log_root = self.project_root / "artifacts" / "control"
        log_root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._log_path = log_root / "{}-{}.log".format(
            timestamp, resolved["scenario_id"]
        )
        self._log_handle = self._log_path.open("a", encoding="utf-8")
        self._control_path = log_root / "{}-{}.control.json".format(
            timestamp, resolved["scenario_id"]
        )
        self._write_control_state("running")
        environment = dict(os.environ)
        environment["OPENPIT_SCENARIO_CONTROL_FILE"] = str(
            self._control_path
        )
        self._process = subprocess.Popen(
            self.command_for(resolved), cwd=str(self.project_root),
            stdin=subprocess.DEVNULL, stdout=self._log_handle,
            stderr=subprocess.STDOUT, start_new_session=True,
            env=environment,
        )
        self._scenario_error = None

    def _write_control_state(self, requested_state: str) -> None:
        if self._control_path is None:
            raise ControlError("Scenario control channel is not initialized")
        payload = {
            "schema_version": "openpit.scenario-process-control.v1",
            "requested_state": str(requested_state),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        temporary = self._control_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self._control_path)

    def _status_locked(self) -> Dict[str, Any]:
        running = self._process is not None and self._process.poll() is None
        result_status = str(
            (self._last_result or {}).get("status") or ""
        ).upper()
        if self._carla_state == "starting" and self._stop_requested:
            state = "stopping"
        elif self._carla_state == "starting":
            state = "starting_carla"
        elif self._carla_state in {"failed", "timeout"}:
            state = "failed"
        elif self._scenario_error:
            state = "failed"
        elif running and self._pause_requested:
            state = "paused"
        elif running and self._stop_requested:
            state = "stopping"
        elif running:
            state = "running"
        elif self._stop_requested and self._request is not None:
            state = "terminated"
        elif result_status == "PARTIAL":
            state = "partial"
        elif result_status in {"PASS", "READY"}:
            state = "completed"
        elif self._request is not None and self._last_exit_code not in (None, 0):
            state = "failed"
        elif self._request is not None and self._last_exit_code == 0:
            state = "completed"
        elif self._request is not None:
            state = "failed"
        else:
            state = "idle"
        return {
            "state": state,
            "running": running,
            "request": dict(self._request) if self._request else None,
            "pid": self._process.pid if running else None,
            "started_at": self._started_at,
            "stop_requested": self._stop_requested,
            "pause_requested": self._pause_requested,
            "exit_code": self._last_exit_code,
            "result": dict(self._last_result) if self._last_result else None,
            "error": self._scenario_error,
            "log_path": str(self._log_path) if self._log_path else None,
            "log_tail": self._read_log_tail(),
            "carla": {
                "state": self._carla_state,
                "pid": (
                    self._carla_process.pid if self._carla_process is not None
                    and self._carla_process.poll() is None else None
                ),
                "log_path": str(self._carla_log_path) if self._carla_log_path else None,
                "error": self._carla_error,
                "launcher_contract": "./CarlaUE4.sh (no extra launch flags)",
            },
        }

    def _read_log_tail(self, limit: int = 30) -> List[str]:
        if self._log_path is None or not self._log_path.exists():
            return []
        try:
            return self._log_path.read_text(
                encoding="utf-8"
            ).splitlines()[-limit:]
        except OSError:
            return []

    def _read_result_summary(self) -> Optional[Dict[str, Any]]:
        """Extract the final JSON object from a mixed CARLA process log."""
        if self._log_path is None or not self._log_path.exists():
            return None
        try:
            text = self._log_path.read_text(encoding="utf-8")
        except OSError:
            return None
        # Convert line numbers into character offsets without assuming that
        # CARLA warnings or actor messages are valid JSON.
        lines = text.splitlines(True)
        offsets, offset = [], 0
        for line in lines:
            if line.startswith("{"):
                offsets.append(offset)
            offset += len(line)
        for start in reversed(offsets):
            try:
                value = json.loads(text[start:].strip())
            except (TypeError, ValueError):
                continue
            if not isinstance(value, dict) or not value.get("status"):
                continue
            closed_loop = value.get("closed_loop_validation") or {}
            route_admission = value.get("route_evidence_admission") or {}
            return {
                "status": value.get("status"),
                "scenario_key": value.get("scenario_key"),
                "scenario_id": value.get("scenario_id"),
                "run_id": value.get("run_id"),
                "task_count": value.get("task_count"),
                "completed_task_count": value.get("completed_task_count"),
                "closed_loop_status": closed_loop.get("status"),
                "scenario_event_applied": value.get(
                    "scenario_event_applied"
                ),
                "route_evidence_admission": route_admission.get("status"),
                "database_path": value.get("database_path"),
            }
        return None
