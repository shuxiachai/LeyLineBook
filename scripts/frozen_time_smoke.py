"""Windows frozen-EXE API/tzdata smoke, not a UI or old-to-new upgrade test.

Run after building: python -B -X utf8 scripts/frozen_time_smoke.py --exe PATH
The executable is read-only; all runtime data and evidence are isolated under
output/playwright. No build, retry, ready signal, or user-instance shutdown occurs.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = (ROOT / "output/playwright").resolve()
INVALID_CHOICE = "\u65e7\u65f6\u95f4\u7684\u65f6\u533a\u6216\u91cd\u590d\u5c0f\u65f6\u9009\u62e9\u65e0\u6548"
INVALID_ZONE = "\u65e7\u65f6\u95f4\u7684\u6765\u6e90\u65f6\u533a\u65e0\u6548\uff0c\u8bf7\u9009\u62e9\u6709\u6548\u7684 IANA \u65f6\u533a"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fixture(local: str, instant: str, offset: int, zone: str = "Australia/Sydney") -> dict:
    return {
        "accounts": [{"id": 1, "name": "Timezone smoke"}],
        "tasks": [{"id": 1, "account_id": 1, "name": "\u8d28\u53d8\u4eea", "recurrence": "interval",
                   "interval_days": 7, "next_due": local}],
        "records": [],
        "timeResolution": {"confirmed": True, "sourceZone": zone, "entries": [{
            "key": "tasks/0/next_due", "original": local, "local": local,
            "instant": instant, "offsetMinutes": offset,
        }]},
    }


class Api:
    def __init__(self, port: int, token: str, report: dict):
        self.base = f"http://127.0.0.1:{port}"
        self.token = token
        self.report = report
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"X-LeyLineBook-Session": self.token, "Content-Type": "application/json", **(headers or {})},
            method="GET" if body is None else "POST",
        )
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = response.status
            payload = json.load(response)
        self.report["responses"].append({"path": path, "status": status, "body": payload})
        return status, payload

    def success(self, path: str, body: dict | None = None) -> dict:
        status, response = self.request(path, body)
        require(status == 200 and response.get("success") is True, f"{path}: {status}, {response!r}")
        return response["data"]

    def verify_identity(self) -> None:
        challenge = secrets.token_hex(32)
        status, response = self.request("/api/instance", headers={
            "X-LeyLineBook-Challenge": challenge, "X-LeyLineBook-Session": "",
        })
        expected = {"application": "LeyLineBook", "protocol": 1, "proof": hmac.new(
            self.token.encode("ascii"), f"LeyLineBook:1:{challenge}".encode("ascii"), hashlib.sha256,
        ).hexdigest()}
        require(status == 200 and response.get("success") is True and response.get("data") == expected,
                "Instance challenge did not identify our newly launched application")


def exercise(api: Api, report: dict) -> None:
    def rejected(name: str, body: dict, error: str) -> None:
        before = api.success("/api/export")["data"]
        status, response = api.request("/api/import", body)
        require(status == 400 and response.get("success") is False and response.get("error") == error,
                f"{name}: expected exact 400 error, got {status}, {response!r}")
        after = api.success("/api/export")["data"]
        require(before == after, f"{name}: rejected import changed exported database data")
        report["cases"].append({"name": name, "passed": True, "status": status, "dataUnchanged": True})

    gap = fixture("2026-10-04T02:30", "2026-10-03T16:30:00Z", 600)
    rejected("Sydney spring gap", gap, INVALID_CHOICE)
    unknown = copy.deepcopy(gap)
    unknown["timeResolution"]["sourceZone"] = "Not/A_Zone"
    rejected("Unknown IANA zone", unknown, INVALID_ZONE)

    for instant, offset in (("2026-04-04T15:30:00Z", 660), ("2026-04-04T16:30:00Z", 600)):
        body = fixture("2026-04-05T02:30", instant, offset)
        api.success("/api/import", body)
        exported = api.success("/api/export")
        data = exported["data"]
        require(len(data["tasks"]) == 1 and data["tasks"][0]["next_due"] == instant,
                "Fold export did not preserve the chosen absolute instant")
        require(data["timeLegacyArchive"] == [body["timeResolution"]],
                "Fold archive did not exactly preserve the submitted resolution")
        state = api.success("/api/state?date=2026-04-05")
        require(state["timeContractVersion"] == 2, "Unexpected time contract version")
        imported_task_id = data["tasks"][0]["id"]
        tasks = [task for task in state["tasks"] if task["id"] == imported_task_id]
        require(len(tasks) == 1, "Imported task missing or duplicated in state")
        task = tasks[0]
        require(task["next_due"] == instant and task["available_at"] == instant
                and task["time_semantics"] == "absolute", "Fold state has incorrect time semantics")
        report["appVersion"] = exported["appVersion"]
        report["cases"].append({"name": f"Sydney fold offset {offset}", "passed": True,
                                "status": 200, "instant": instant, "archiveExact": True, "stateExact": True})

    rejected("Fold offset mismatch", fixture("2026-04-05T02:30", "2026-04-04T15:30:00Z", 600), INVALID_CHOICE)


def wait_for_start(process: subprocess.Popen, temporary: Path, data: Path, port: int, api: Api) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        require(process.poll() is None, f"Frozen EXE exited during startup: {process.returncode}")
        # Logs are diagnostic only; their Windows encoding and startup wording
        # are not readiness contracts. The public challenge never sends the token.
        logs = (data / "task_recorder.log", data.parent / "process.log")
        contents = b"\n".join(log.read_bytes() for log in logs if log.exists())
        require(b"selecting another local port" not in contents, "Port collision: application attempted a fallback port")
        ports = re.findall(rb"http://127\.0\.0\.1:(\d+)", contents)
        require(all(int(value) == port for value in ports), "Application startup log contains a different port")
        instances = list(temporary.glob("LeyLineBook-session-*.txt"))
        require(all(path.name == f"LeyLineBook-session-{port}.txt" for path in instances),
                "Application created an instance marker for a fallback port")
        if instances:
            try:
                api.verify_identity()
            except urllib.error.URLError as error:
                if not isinstance(error.reason, ConnectionRefusedError):
                    raise
            else:
                api.report["identityVerified"] = True
                api.report["instanceMarker"] = str(instances[0])
                return
        time.sleep(0.1)
    raise RuntimeError("No authenticated instance and isolated marker within 45 seconds")


def runtime_resources(temporary: Path) -> dict:
    candidates = list(temporary.glob("_MEI*/tzdata/zoneinfo/Australia/Sydney"))
    require(len(candidates) == 1, f"Expected one bundled Sydney timezone resource, found {candidates!r}")
    sydney = candidates[0]
    require(sydney.read_bytes().startswith(b"TZif"), "Bundled Sydney resource is not TZif data")
    metadata = sydney.parents[1] / "tzdata.zi"
    version = metadata.read_text(encoding="utf-8").splitlines()[0]
    require(version.startswith("# version "), "Bundled tzdata lacks IANA version evidence")
    return {"sydneyPath": str(sydney), "sydneySha256": digest(sydney),
            "sydneyBytes": sydney.stat().st_size, "ianaVersion": version.removeprefix("# version "),
            "metadataSha256": digest(metadata), "pythonTzpath": "", "hostSitePackagesDisabled": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True, help="Already built current-source Windows executable")
    args = parser.parse_args()
    if os.name != "nt":
        print("Windows required; no frozen smoke executed.", file=sys.stderr)
        return 2
    executable = args.exe.resolve(strict=True)
    require(executable.is_file() and executable.suffix.lower() == ".exe", "--exe must be an existing executable")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="frozen-time-", dir=OUTPUT)).resolve(strict=True)
    require(directory.parent == OUTPUT, "Runtime output escaped the isolated workspace directory")
    temporary = directory / "temp"
    data = directory / "data"
    temporary.mkdir()
    data.mkdir()
    report = {"level": "real-new-frozen-exe-authenticated-api-and-bundled-tzdata", "passed": False,
              "uiTest": False, "oldToNewUpgrade": False, "fakeReady": False,
              "executable": str(executable), "executableSha256": digest(executable),
              "output": str(directory), "database": str(data / "task_records.db"),
              "temporary": str(temporary), "cases": [], "responses": []}
    print(f"Frozen API/tzdata evidence: {directory}", flush=True)
    process = None
    api = None
    verified = False
    try:
        token = secrets.token_hex(32)
        env = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary),
               "LEYLINEBOOK_DATA_DIR": str(data), "LEYLINEBOOK_SESSION_TOKEN": token,
               "PYTHONTZPATH": "", "PYTHONNOUSERSITE": "1"}
        for key in ("PYTHONHOME", "PYTHONPATH", "TASK_RECORDER_PORT"):
            env.pop(key, None)
        # An exclusive bind proves availability immediately before launch. The fresh
        # TEMP directory prevents main() from authenticating/stopping any old instance.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
            require(port != 8765, "Refusing the normal user application port")
            require(not list(temporary.iterdir()), "Isolated TEMP was not empty before launch")
        report["port"] = port
        with (directory / "process.log").open("wb") as log:
            process = subprocess.Popen([str(executable), "--no-browser", "--port", str(port)],
                                       cwd=directory, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            report["launcherPid"] = process.pid
            api = Api(port, token, report)
            wait_for_start(process, temporary, data, port, api)
            verified = True
            report["resources"] = runtime_resources(temporary)
            exercise(api, report)
            require((data / "task_records.db").is_file(), "No isolated application database was created")
            require(digest(executable) == report["executableSha256"], "Input executable changed during smoke")
            report["passed"] = True
    except Exception as error:
        report["error"] = repr(error)
        report["traceback"] = traceback.format_exc()
    finally:
        if process is not None:
            try:
                if process.poll() is None and verified:
                    api.verify_identity()
                    api.success("/api/shutdown", {})
                    process.wait(timeout=10)
                if process.poll() is None:
                    report["forcedOwnedProcessCleanup"] = True
                    subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
                    process.wait(timeout=10)
                report["exitCode"] = process.returncode
                require(not report.get("passed") or process.returncode == 0, "Frozen EXE did not exit successfully")
            except Exception as error:
                report["passed"] = False
                report["cleanupError"] = repr(error)
                if process.poll() is None:
                    subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
                    process.wait(timeout=10)
        (directory / "report.json").write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "responses"}, ensure_ascii=True, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
