import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from open_pit_agent.decision import CandidateCostInput, MapContext, MultiObjectiveCostModel, OptimizationScheduler
from open_pit_agent.models import Position, Task, VehicleState


def vehicle(name):
    return VehicleState(name, name, "truck", "haul", "bp", ["haul"], Position(0, 0))


class OptimizationSchedulerTests(unittest.TestCase):
    def test_global_assignment_is_unique_and_respects_hard_constraints(self):
        model = MultiObjectiveCostModel({"cost_transport": 1.0})
        scheduler = OptimizationScheduler(model)
        t1, t2 = Task("t1", "z1", 1, ["haul"]), Task("t2", "z2", 1, ["haul"])
        v1, v2 = vehicle("v1"), vehicle("v2")
        inputs = {
            "t1": [CandidateCostInput(v1, t1, MapContext(True, 100)),
                   CandidateCostInput(v2, t1, MapContext(False, None))],
            "t2": [CandidateCostInput(v1, t2, MapContext(True, 100)),
                   CandidateCostInput(v2, t2, MapContext(True, 200))],
        }
        result = scheduler.optimize(inputs)
        self.assertEqual("OPTIMAL", result.status)
        self.assertEqual({"v1", "v2"}, {item["vehicle_id"] for item in result.assignments})
        self.assertEqual(2, len(result.safety_reviews))
        self.assertTrue(all(
            item["status"] == "APPROVED" for item in result.safety_reviews
        ))
        self.assertTrue(all(
            item["safety_review"]["status"] == "APPROVED"
            for item in result.assignments
        ))
        rejected = result.candidate_rankings["t1"][1]
        self.assertFalse(rejected["feasible"])
        self.assertIsNone(rejected["total_cost"])


if __name__ == "__main__":
    unittest.main()
