"""Optional real-source WebView2 smoke; not an old-EXE to new-EXE upgrade.

Run on Windows: python -B -X utf8 scripts/windows_ui_smoke.py
Only an isolated child process, its hidden native window and test DB are used.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = (ROOT / "output/playwright").resolve()


def child(directory: Path) -> int:
    directory = directory.resolve(strict=True)
    if directory.parent != OUTPUT or not directory.name.startswith("native-ui-"):
        raise RuntimeError("Native smoke requires its own output/playwright fixture directory")
    data = directory / "data"
    data.mkdir(exist_ok=False)
    os.environ["LEYLINEBOOK_DATA_DIR"] = str(data)
    sys.path.insert(0, str(ROOT))
    import app
    import webview

    health = Path(tempfile.gettempdir()) / ("LeyLineBook-update-health-" + secrets.token_hex(8) + ".ok")
    result = {"level": "real-source-native-webview2", "packagedUpgrade": False, "database": str(app.DB_PATH), "health": str(health), "passed": False}
    original_create = webview.create_window
    original_launch = app.launch_window

    def hidden_window(*args, **kwargs):
        kwargs["hidden"] = True
        return original_create(*args, **kwargs)

    def native_only(url):
        if not original_launch(url):
            raise RuntimeError("Native window failed; browser fallback is not accepted by this smoke")
        return True

    webview.create_window = hidden_window
    app.launch_window = native_only

    def observe_ready() -> None:
        try:
            deadline = time.monotonic() + 30
            while not health.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not health.is_file():
                raise RuntimeError("No real frontend /api/ready within 30 seconds")
            if webview.renderer != "edgechromium":
                raise RuntimeError(f"Unexpected native renderer: {webview.renderer}")
            observed = webview.windows[0].evaluate_js("({webview2: Boolean(window.chrome && window.chrome.webview), pwa: Boolean(window.LOCAL_BACKEND), loaded: Boolean(state.data && !state.loadingDate), selectedDate: document.querySelector('#selectedDate').value, title: document.title})")
            if not observed or not observed["webview2"] or observed["pwa"] or not observed["loaded"] or not observed["selectedDate"]:
                raise RuntimeError(f"Native application did not become usable: {observed!r}")
            result.update({"passed": True, "renderer": webview.renderer, "observed": observed, "healthContents": health.read_text()})
        except Exception as error:
            result["error"] = repr(error)
        finally:
            (directory / "report.json").write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
            if webview.windows:
                webview.windows[0].destroy()

    observer = threading.Thread(target=observe_ready, daemon=True)
    observer.start()
    try:
        # Port zero never probes or shuts down an existing user instance.
        app.run_server(0, "window", str(health))
        observer.join(timeout=5)
    except Exception as error:
        result["error"] = repr(error)
        result["passed"] = False
        (directory / "report.json").write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
    return 0 if result["passed"] else 1


def main() -> int:
    if os.name != "nt":
        print("Windows/WebView2 required; no native smoke executed.", file=sys.stderr)
        return 2
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        return child(Path(sys.argv[2]))
    if len(sys.argv) != 1:
        raise RuntimeError("Run without arguments; output is allocated by the smoke")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="native-ui-", dir=OUTPUT))
    temporary = directory / "temp"
    temporary.mkdir()
    env = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "LEYLINEBOOK_SESSION_TOKEN": secrets.token_hex(32)}
    print(f"Native source UI evidence: {directory}", flush=True)
    with (directory / "native.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()), "--child", str(directory)], cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            code = process.wait(timeout=45)
        except subprocess.TimeoutExpired:
            # The PID belongs to this newly created isolated child, not a user app.
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10)
            process.wait(timeout=10)
            raise RuntimeError(f"Native child timed out; inspect {directory / 'native.log'}")
    report = directory / "report.json"
    if report.exists():
        print(report.read_text(encoding="utf-8"))
    else:
        print(f"No native report produced; inspect {directory / 'native.log'}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
