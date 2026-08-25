import importlib.util
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = (
    PROJECT_ROOT / "scripts" / "launch_desktop.py"
)
SPEC = importlib.util.spec_from_file_location(
    "launch_desktop", str(LAUNCHER_PATH)
)
launch_desktop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launch_desktop)


class DesktopLauncherTest(unittest.TestCase):
    def test_browser_command_uses_app_window_mode(self):
        command = launch_desktop.browser_command(
            "/usr/bin/google-chrome",
            "http://127.0.0.1:12345",
            "/tmp/project-window-profile",
        )

        self.assertIn(
            "--app=http://127.0.0.1:12345", command
        )
        self.assertIn(
            "--user-data-dir=/tmp/project-window-profile",
            command,
        )
        self.assertNotIn("--start-fullscreen", command)

    def test_fullscreen_is_explicit(self):
        command = launch_desktop.browser_command(
            "/usr/bin/google-chrome",
            "http://127.0.0.1:12345",
            "/tmp/project-window-profile",
            fullscreen=True,
        )

        self.assertIn("--start-fullscreen", command)


if __name__ == "__main__":
    unittest.main()
