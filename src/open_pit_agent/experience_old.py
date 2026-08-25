"""
Experience memory builder.

Convert successful simulation runs into reusable scheduling experience.
"""

import json
from pathlib import Path
from datetime import datetime


class ExperienceMemory:

    def __init__(self, path=None):

        if path is None:
            path = (
                Path(__file__).parents[2]
                /
                "artifacts"
                /
                "experience_memory.json"
            )

        self.path = Path(path)

        if self.path.exists():

            with open(
                self.path,
                "r",
                encoding="utf-8"
            ) as f:

                self.memory = json.load(f)

        else:

            self.memory = []


    def evaluate(self, summary):

        """
        Evaluate whether this run can become experience.
        """

        tasks = summary.get(
            "tasks",
            []
        )

        if not tasks:

            return None


        completed = 0

        for task in tasks:

            if task.get(
                "status"
            ) in [
                "completed",
                "success"
            ]:

                completed += 1


        success_rate = (
            completed /
            len(tasks)
        )


        if success_rate < 0.5:

            return None


        experience = {

            "time":
                datetime.now().isoformat(),


            "scenario":
                summary.get(
                    "scenario_id",
                    ""
                ),


            "vehicles":
                summary.get(
                    "vehicle_states",
                    []
                ),


            "tasks":
                tasks,


            "reward":
                round(
                    success_rate,
                    3
                ),


            "description":
                "successful scheduling case"

        }


        return experience



    def add(self, experience):

        if experience is None:

            return


        self.memory.append(
            experience
        )


        with open(
            self.path,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                self.memory,
                f,
                ensure_ascii=False,
                indent=4
            )
