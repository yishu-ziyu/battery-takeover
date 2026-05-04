from __future__ import annotations

from pathlib import Path
import unittest


class ReleaseWorkflowTests(unittest.TestCase):
    def test_installer_workflow_runs_unit_tests_before_packaging(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = root / ".github" / "workflows" / "build-macos-installer.yml"
        text = workflow.read_text(encoding="utf-8")

        test_pos = text.find("python3 -m unittest discover -s tests -v")
        build_pos = text.find("./build_macos_installer.sh")

        self.assertNotEqual(test_pos, -1)
        self.assertNotEqual(build_pos, -1)
        self.assertLess(test_pos, build_pos)


if __name__ == "__main__":
    unittest.main()
