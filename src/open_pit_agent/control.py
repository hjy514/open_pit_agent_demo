"""Allowlisted local process control for demonstration scenarios."""

import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


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
