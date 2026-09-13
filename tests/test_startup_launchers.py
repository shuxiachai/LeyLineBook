import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VBS = "\u542f\u52a8\u5730\u8109\u7c3f.vbs"
BAT = "\u542f\u52a8\u5730\u8109\u7c3f.bat"


class StartupLauncherTests(unittest.TestCase):
    def test_wrappers_delegate_without_a_second_http_shutdown_protocol(self):
        for filename in (VBS, BAT, "start_app.ps1"):
            with self.subTest(file=filename):
                text = (ROOT / filename).read_text(encoding="utf-8-sig")
                self.assertNotRegex(text.lower(), r"/api/|xmlhttp|invoke-restmethod|invoke-webrequest|127\.0\.0\.1")
        self.assertIn("\\app.py", (ROOT / VBS).read_text())
        self.assertIn('"app.py"', (ROOT / "start_app.ps1").read_text())
        self.assertIn(VBS, (ROOT / BAT).read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "Windows launcher execution requires Windows")
    def test_double_click_wrappers_and_powershell_start_only_the_isolated_app(self):
        with tempfile.TemporaryDirectory(prefix="leylinebook-launcher-test-") as temp:
            directory = Path(temp)
            venv.EnvBuilder(with_pip=False).create(directory / ".venv")
            for filename in (VBS, BAT, "start_app.ps1"):
                shutil.copyfile(ROOT / filename, directory / filename)
            (directory / "app.py").write_text(
                "import json, sys\nfrom pathlib import Path\n"
                "Path(__file__).with_name('launched.json').write_text(json.dumps(sys.argv[1:]))\n",
                encoding="ascii",
            )
            commands = [
                (["cscript.exe", "//B", "//Nologo", str(directory / VBS)], []),
                (["cmd.exe", "/d", "/c", BAT], []),
                (["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(directory / "start_app.ps1"), "-NoBrowser"], ["--no-browser"]),
            ]
            for command, expected in commands:
                with self.subTest(launcher=command[0]):
                    marker = directory / "launched.json"
                    marker.unlink(missing_ok=True)
                    completed = subprocess.run(command, cwd=directory, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
                    deadline = time.monotonic() + 10
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(marker.exists(), f"No isolated launch evidence from {command[0]}")
                    self.assertEqual(json.loads(marker.read_text()), expected)


if __name__ == "__main__":
    unittest.main()
