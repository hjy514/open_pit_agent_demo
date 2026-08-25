import json
from pathlib import Path

from src.open_pit_agent.experience import ExperienceMemory


root = Path(
    "artifacts/runs"
)


runs = sorted(
    root.iterdir(),
    key=lambda x:x.stat().st_mtime
)


latest = runs[-1]


summary_file = (
    latest
    /
    "summary.json"
)


with open(
    summary_file,
    "r",
    encoding="utf-8"
) as f:

    summary=json.load(f)



memory = ExperienceMemory()

exp = memory.evaluate(
    summary
)

memory.add(
    exp
)


print(
    "experience generated:",
    exp is not None
)

print(
    "memory size:",
    len(memory.memory)
)
