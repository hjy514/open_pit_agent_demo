"""
Export backend simulation result to dashboard interface.

open_pit_agent_demo
            |
            |
            v
open_pit_dispatch_app/data
"""

import json
import os
from pathlib import Path
from datetime import datetime


DEFAULT_TARGET = Path(
    os.environ.get(
        "OPENPIT_DISPATCH_DATA_DIR",
        str(Path.home() / "open_pit_dispatch_app" / "data"),
    )
).expanduser()


def export_interface(summary, recorder):

    target = DEFAULT_TARGET

    target.mkdir(
        parents=True,
        exist_ok=True
    )


    # =========================
    # vehicle
    # =========================

    vehicles = []

    for item in summary.get(
        "vehicle_states",
        []
    ):

        pos = item.get(
            "position",
            {}
        )

        vehicles.append(
            {
                "id":
                    item.get(
                        "vehicle_id",
                        ""
                    ),

                "display_name":
                    item.get(
                        "display_name",
                        ""
                    ),

                "type":
                    item.get(
                        "equipment_type",
                        ""
                    ),

                "status":
                    item.get(
                        "task_status",
                        "unknown"
                    ),

                "speed":
                    "{:.1f} km/h".format(
                        item.get(
                            "speed_mps",
                            0
                        ) * 3.6
                    ),

                "position":
                    "({}, {}, {})".format(
                        pos.get("x",0),
                        pos.get("y",0),
                        pos.get("z",0)
                    ),

                "task":
                    item.get(
                        "current_task_id",
                        ""
                    ),

                "health":
                    item.get(
                        "health",
                        ""
                    ),

                "communication":
                    "online"
                    if item.get(
                        "available",
                        False
                    )
                    else
                    "offline",

                "source":
                    "AI"
            }
        )


    write_json(
        target/"vehicle_state.json",
        vehicles
    )


    # =========================
    # Agent状态
    # =========================

    agent = {

        "agents":[

            {
                "name":"Risk Agent",
                "status":"ONLINE",
                "function":"风险评估"
            },

            {
                "name":"Scheduler Agent",
                "status":"ONLINE",
                "function":"任务动态调度"
            },

            {
                "name":"Memory Agent",
                "status":"ONLINE",
                "function":"历史经验学习"
            }

        ],

        "environment":
            summary.get(
                "mission",
                {}
            ),

        "risk":

            {
                "level":
                    "UNKNOWN",

                "assessment":
                    summary.get(
                        "risk_assessments",
                        []
                    )
            },


        "decision":
            {
                "status":
                    summary.get(
                        "status",
                        ""
                    )
            }
    }


    write_json(
        target/"agent_state.json",
        agent
    )


    # =========================
    # 调度状态
    # =========================

    dispatch = {

        "tasks":
            summary.get(
                "tasks",
                []
            ),

        "zones":
            summary.get(
                "zones",
                []
            ),

        "run_id":
            summary.get(
                "run_id",
                ""
            ),

        "time":
            datetime.now().isoformat()
    }


    write_json(
        target/"dispatch_state.json",
        dispatch
    )


    # =========================
    # 运行结果
    # =========================

    write_json(
        target/"evaluation_result.json",
        summary
    )


    print(
        "[Interface Export]",
        target
    )



def write_json(path, data):

    with open(
        path,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=4
        )
