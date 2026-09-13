import json
import os
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
                        script = "$ErrorActionPreference = 'Stop'\n" + "\n".join(script) + "\nWrite-Output 'LATER_UPLOAD_REACHED'\nexit $LASTEXITCODE"
                        result = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, timeout=15)
                        self.assertEqual(result.returncode, 7, result.stderr.decode(errors="replace"))
                        self.assertNotIn(b"LATER_UPLOAD_REACHED", result.stdout)

    def _release_upload_step(self):
        text = (Path(__file__).resolve().parents[1] / ".github/workflows/release.yml").read_text(encoding="utf-8")
        match = re.search(
            r"^      - name: Upload assets to existing draft Release\n(.*?)(?=^      - name: |\Z)",
            text, re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(match)
        block = re.search(r"^        run: \|\n((?: {10}[^\n]*\n?)+)", match[1], re.MULTILINE)
        self.assertIsNotNone(block)
        return text, "\n".join(line[10:] for line in block[1].splitlines())

    def test_release_upload_cannot_create_publish_or_bypass_tests(self):
        text, script = self._release_upload_step()
        self.assertEqual(re.findall(r"\bgh\s+release\s+(\w+)", text), ["view", "upload"])
        self.assertNotRegex(text, r"--(?:body|notes|generate-notes|draft|latest)\b")
        self.assertNotRegex(text, r"(?m)^\s*(?:if|continue-on-error):")
        self.assertIn("'^v(\\d+\\.\\d+\\.\\d+)$'", text)
        steps = re.findall(r"^      - name: (.+)$", text, re.MULTILINE)
        sequence = ["Validate release version", "Run test suites", "Run browser regressions",
                    "Run isolated Windows upgrade process tests", "Build Windows executable",
                    "Smoke-test packaged timezone data", "Smoke-test packaged executable",
                    "Name asset and create SHA-256 file", "Upload assets to existing draft Release"]
        for step in sequence:
            self.assertEqual(steps.count(step), 1, step)
        positions = [steps.index(step) for step in sequence]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(steps[-1], sequence[-1])
        self.assertEqual(script.count("--clobber"), 1)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell Core is required for Windows workflow checks")
    def test_release_upload_requires_exact_tag_and_boolean_draft(self):
        _, script = self._release_upload_step()
        draft = {"isDraft": True, "tagName": "v3.0.6"}
        cases = [
            ("draft", json.dumps(draft), 0, 0, True),
            ("multiline draft", json.dumps(draft, indent=2), 0, 0, True),
            ("missing", "", 1, 0, False),
            ("query error", json.dumps(draft), 7, 0, False),
            ("published", json.dumps({**draft, "isDraft": False}), 0, 0, False),
            ("invalid JSON", "not JSON", 0, 0, False),
            ("empty JSON", "", 0, 0, False),
            ("null", "null", 0, 0, False),
            ("empty object", "{}", 0, 0, False),
            ("array", json.dumps([draft]), 0, 0, False),
            ("nested array", json.dumps([[draft]]), 0, 0, False),
            ("boolean", "true", 0, 0, False),
            ("string", json.dumps("true"), 0, 0, False),
            ("missing draft", json.dumps({"tagName": "v3.0.6"}), 0, 0, False),
            ("missing tag", json.dumps({"isDraft": True}), 0, 0, False),
            ("wrong tag", json.dumps({**draft, "tagName": "v3.0.5"}), 0, 0, False),
            ("wrong tag case", json.dumps({**draft, "tagName": "V3.0.6"}), 0, 0, False),
            ("upload failure", json.dumps(draft), 0, 7, False),
        ]
        for value in ("true", 1, None, [True], {"value": True}):
            cases.append((f"invalid draft {value!r}", json.dumps({**draft, "isDraft": value}), 0, 0, False))
        for value in (True, 306, None, ["v3.0.6"], {"value": "v3.0.6"}):
            cases.append((f"invalid tag {value!r}", json.dumps({**draft, "tagName": value}), 0, 0, False))
        mock = """
$ErrorActionPreference = 'Stop'
function gh {
    [Console]::WriteLine('MOCK_GH ' + (ConvertTo-Json -Compress -InputObject @($args)))
    if ($args[0] -cne 'release') { throw 'Unexpected gh command' }
    if ($args[1] -ceq 'view') {
        $global:LASTEXITCODE = [int]$env:MOCK_QUERY_EXIT
        Write-Output ($env:MOCK_RELEASE_JSON -split "`n")
    } elseif ($args[1] -ceq 'upload') {
        $global:LASTEXITCODE = [int]$env:MOCK_UPLOAD_EXIT
    } else { throw 'Unexpected gh release mutation' }
}
"""
        for name, body, query_exit, upload_exit, succeeds in cases:
            with self.subTest(case=name):
                env = dict(os.environ, PATH="", GH_TOKEN="", GITHUB_TOKEN="",
                           TAG_NAME="v3.0.6", ASSET_NAME="fixture.exe", MOCK_RELEASE_JSON=body,
                           MOCK_QUERY_EXIT=str(query_exit), MOCK_UPLOAD_EXIT=str(upload_exit))
                result = subprocess.run(
                    [shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command",
                     mock + script + "\nWrite-Output 'DRAFT_UPLOAD_COMPLETE'\nexit $LASTEXITCODE"],
                    env=env, capture_output=True, text=True, encoding="utf-8", timeout=15,
                )
                calls = [json.loads(line.removeprefix("MOCK_GH "))
                         for line in result.stdout.splitlines() if line.startswith("MOCK_GH ")]
                expected = [["release", "view", "v3.0.6", "--json", "isDraft,tagName"]]
                if succeeds or name == "upload failure":
                    asset = os.path.join("dist", "fixture.exe")
                    expected.append(["release", "upload", "v3.0.6", asset, asset + ".sha256", "--clobber"])
                self.assertEqual(calls, expected, result.stderr)
                self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                self.assertEqual("DRAFT_UPLOAD_COMPLETE" in result.stdout, succeeds, result.stderr)

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
