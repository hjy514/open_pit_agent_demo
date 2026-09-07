import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
LAUNCHER_PATH = SCRIPTS_ROOT / "launch_demo.py"
SPEC = importlib.util.spec_from_file_location(
    "launch_demo", str(LAUNCHER_PATH)
)
launch_demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launch_demo)


class CombinedDemoLauncherTest(unittest.TestCase):
    def test_carla_command_is_fixed_argument_list(self):
        config_root = (
            PROJECT_ROOT
            / "configs"
            / "open_pit_mine.example.json"
        ).parent
        with self.assertRaises(
            launch_demo.DemoLaunchError
        ):
            launch_demo.carla_command(config_root)

        with tempfile.TemporaryDirectory() as temp_dir:
            fake_root = Path(temp_dir)
            (fake_root / "CarlaUE4.sh").write_text(
                "#!/bin/sh\n", encoding="utf-8"
            )
            command = launch_demo.carla_command(
                fake_root
            )
            self.assertEqual(
                str(fake_root / "CarlaUE4.sh"),
                command[0],
            )
            self.assertEqual(
                [], command[1:]
            )

    def test_desktop_command_forwards_fullscreen_only(self):
        normal = launch_demo.desktop_command(
            "/safe/python"
        )
        fullscreen = launch_demo.desktop_command(
            "/safe/python", fullscreen=True
        )

        self.assertNotIn("--fullscreen", normal)
        self.assertIn("--fullscreen", fullscreen)
        self.assertEqual("/safe/python", fullscreen[0])


if __name__ == "__main__":
    unittest.main()
