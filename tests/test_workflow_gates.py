import re
import shutil
import subprocess
import unittest
from pathlib import Path


class WorkflowGateTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell Core is required for Windows workflow checks")
    def test_every_native_command_failure_stops_its_workflow_step(self):
        root = Path(__file__).resolve().parents[1] / ".github/workflows"
        cases = {
            "ci.yml": ("Install dependencies", "Check source syntax", "Smoke-test packaged timezone data"),
            "release.yml": ("Install dependencies", "Run test suites", "Smoke-test packaged timezone data"),
        }
        for filename, steps in cases.items():
            text = (root / filename).read_text(encoding="utf-8")
            for step in steps:
                # Extract the literal shell block, then replace only native commands
                # with synthetic exit codes. Nothing is installed, built or published.
                match = re.search(r"- name: " + re.escape(step) + r"\n\s+run: \|\n((?: {10}[^\n]*\n)+)", text)
                self.assertIsNotNone(match, step)
                lines = [line[10:] for line in match[1].splitlines()]
                positions = [i for i, line in enumerate(lines) if line.startswith(("python ", "npm ", "node "))]
                self.assertTrue(positions)
                for failure in positions:
                    with self.subTest(workflow=filename, step=step, command=lines[failure]):
                        script = list(lines)
                        for i in positions:
                            script[i] = f'& "{shutil.which("python")}" -c "raise SystemExit({7 if i == failure else 0})"'
                        script = "$ErrorActionPreference = 'Stop'\n" + "\n".join(script) + "\nexit $LASTEXITCODE"
                        result = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, timeout=15)
                        self.assertEqual(result.returncode, 7, result.stderr.decode(errors="replace"))

    def test_frozen_timezone_gate_uses_built_exe_before_distribution(self):
        root = Path(__file__).resolve().parents[1] / ".github/workflows"
        destinations = {
            "ci.yml": "Upload packaged executable",
            "release.yml": "Name asset and create SHA-256 file",
        }
        for filename, destination in destinations.items():
            with self.subTest(workflow=filename):
                text = (root / filename).read_text(encoding="utf-8")
                steps = re.findall(r"^      - name: (.+)$", text, re.MULTILINE)
                sequence = ["Build Windows executable", "Smoke-test packaged timezone data",
                            "Smoke-test packaged executable", destination]
                for step in sequence:
                    self.assertEqual(steps.count(step), 1, step)
                positions = [steps.index(step) for step in sequence]
                self.assertEqual(positions, sorted(positions))
                match = re.search(
                    r"^      - name: Smoke-test packaged timezone data\n(.*?)(?=^      - name: |\Z)",
                    text, re.MULTILINE | re.DOTALL,
                )
                self.assertIsNotNone(match)
                self.assertEqual(match[1].strip().splitlines(), [
                    "run: |",
                    "          python -B -X utf8 scripts/frozen_time_smoke.py --exe dist/LeyLineBook.exe",
                    "          if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
                ])
