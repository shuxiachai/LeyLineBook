"""Real v3.0.5/candidate EXE headless first-start and isolated recovery smoke.

This never runs an updater batch, UI, browser, or /api/ready. A health-file
argument selects real frozen automatic recovery; no health file is expected.
Builds and retries are deliberately outside this script. Review before use.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request

from frozen_time_smoke import Api as FrozenApi, digest, wait_for_start


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "playwright"
OLD_NAME = "LeyLineBook-v3.0.5-Windows-x64.exe"
OLD_SHA256 = "590e1f54a898341c6ab1a1716505d51552aef6ec582afcace8e172b9aa4f8b95"
OLD_URL = "https://github.com/shuxiachai/LeyLineBook/releases/download/v3.0.5/" + OLD_NAME
CHECKPOINT = "startup_time_contract_v2"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def contained(path: Path, root: Path) -> Path:
    require(path.is_absolute() and path.resolve() == path, f"Redirected/non-absolute fixture path: {path}")
    require(root == path or root in path.parents, f"Fixture escaped its allocated directory: {path}")
    return path


def copy_new(source: Path, target: Path, expected_hash: str) -> None:
    require(source.is_file() and digest(source) == expected_hash, f"Input digest mismatch: {source}")
    with source.open("rb") as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst)
    require(digest(target) == expected_hash, f"Copied digest mismatch: {target}")


def blob_json(value):
    if isinstance(value, bytes):
        return {"sqliteBlobBase64": base64.b64encode(value).decode("ascii")}
    raise TypeError(type(value).__name__)


def logical_database(database: Path) -> dict:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
                f"Database integrity_check failed: {database}")
        schema = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        tables = {}
        for kind, name, _, _ in schema:
            if kind == "table":
                quoted = '"' + name.replace('"', '""') + '"'
                rows = connection.execute("SELECT * FROM " + quoted).fetchall()
                tables[name] = sorted(json.dumps(row, ensure_ascii=True, default=blob_json) for row in rows)
        return {"schema": schema, "tables": tables}


def logical_summary(contents: dict) -> dict:
    encoded = json.dumps(contents, sort_keys=True, ensure_ascii=True).encode("ascii")
    return {"sha256": hashlib.sha256(encoded).hexdigest(),
            "tableRows": {name: len(rows) for name, rows in contents["tables"].items()}}


def checkpoint(database: Path) -> dict | None:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='app_meta' AND type='table'").fetchone():
            return None
        row = connection.execute("SELECT value FROM app_meta WHERE key=?", (CHECKPOINT,)).fetchone()
        return json.loads(row[0]) if row else None


def database_files(database: Path) -> dict:
    return {suffix: digest(path) for suffix in ("", "-wal", "-shm", "-journal")
            if (path := database.with_name(database.name + suffix)).exists()}


def archive_application_log(data: Path, directory: Path) -> Path | None:
    source = contained(data / "task_recorder.log", data)
    if not source.exists():
        return None
    target = contained(directory / "application.log", directory)
    require(source.is_file() and not target.exists(), "Application log archive would overwrite evidence")
    source.rename(target)
    return target


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Api(FrozenApi):
    def __init__(self, port: int, token: str, report: dict):
        super().__init__(port, token, report)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), RejectRedirects())
        self.verified = False

    def request(self, path: str, body: dict | None = None, headers: dict | None = None,
                method: str | None = None) -> tuple[int, dict]:
        require(path.startswith("/api/") and not path.startswith("//"), "Only local application API paths are allowed")
        require(path.split("?", 1)[0] not in {"/api/ready", "/api/update/check", "/api/update/apply"},
                "UI readiness and online updater APIs are outside this headless smoke")
        require(path == "/api/instance" or self.verified, "Instance must be verified before sending its session token")
        method = method or ("GET" if body is None else "POST")
        request = urllib.request.Request(
            self.base + path, data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"X-LeyLineBook-Session": self.token, "Content-Type": "application/json", **(headers or {})},
            method=method,
        )
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            require(not 300 <= response.status < 400, f"Local API redirect rejected: HTTP {response.status}")
            status, payload = response.status, json.load(response)
        self.report["responses"].append({"path": path, "method": method, "status": status,
                                         "success": payload.get("success")})
        return status, payload

    def verify_identity(self) -> None:
        super().verify_identity()
        self.verified = True

    def success(self, path: str, body: dict | None = None, *, method: str | None = None,
                expected_status: int = 200):
        status, response = self.request(path, body, method=method)
        require(status == expected_status and response.get("success") is True,
                f"{path}: unexpected status/success ({status}, {response.get('success')})")
        return response["data"]


@contextmanager
def headless(executable: Path, data: Path, directory: Path, report: dict, *, automatic: bool = False):
    directory.mkdir()
    require(not (data / "task_recorder.log").exists(), "Previous application log was not archived; refusing another stage")
    temporary = directory / "temp"
    temporary.mkdir()
    token = secrets.token_hex(32)
    env = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary),
           "LEYLINEBOOK_DATA_DIR": str(data), "LEYLINEBOOK_SESSION_TOKEN": token,
           "PYTHONNOUSERSITE": "1", "PYTHONTZPATH": ""}
    for key in list(env):
        if key in {"PYTHONHOME", "PYTHONPATH", "TASK_RECORDER_PORT", "BROWSER"} or key.startswith("WEBVIEW2_"):
            env.pop(key)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
        require(port not in {8765, 18765, 9222}, "Refusing a conventional application/debugging port")
        require(not list(temporary.iterdir()), "TEMP is not empty before launch")
    env["TASK_RECORDER_PORT"] = str(port)
    health = temporary / ("LeyLineBook-update-health-" + secrets.token_hex(8) + ".ok")
    args = [str(executable), "--no-browser", "--port", str(port)]
    if automatic:
        args += ["--update-health-file", str(health)]
    evidence = {"directory": str(directory), "executable": str(executable), "port": port,
                "automatic": automatic, "health": str(health), "responses": []}
    report["processes"].append(evidence)
    api = Api(port, token, evidence)
    process = None
    completed = False
    started = time.monotonic()
    try:
        with (directory / "process.log").open("xb") as log:
            process = subprocess.Popen(args, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            evidence["launcherPid"] = process.pid
            wait_for_start(process, temporary, data, port, api)
            yield api, health
            completed = True
    finally:
        try:
            if process is not None and process.poll() is None and api.verified:
                api.verify_identity()
                api.success("/api/shutdown", {})
                process.wait(timeout=10)
        finally:
            if process is not None and process.poll() is None:
                evidence["forcedOwnedProcessCleanup"] = True
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
                process.wait(timeout=10)
            evidence["exitCode"] = None if process is None else process.returncode
            evidence["seconds"] = round(time.monotonic() - started, 3)
            evidence["healthAbsent"] = not health.exists()
            if process is not None and process.poll() is not None:
                archived = archive_application_log(data, directory)
                evidence["applicationLog"] = str(archived) if archived else None
            require(not health.exists(), "Headless execution unexpectedly produced UI readiness")
            if completed and process is not None:
                require(process.returncode == 0 and not evidence.get("forcedOwnedProcessCleanup"),
                        "Headless application did not shut down cleanly")


def seed_old(api: Api) -> tuple[dict, dict]:
    require(api.success("/api/export")["appVersion"] == "3.0.5", "Seed is not the official old application version")
    account = api.success("/api/accounts", {"name": "Old EXE recovery fixture", "owner": "Synthetic only",
                                           "notes": "Created through released v3.0.5 API"}, expected_status=201)
    credentials = {"username": "synthetic-upgrade-user", "password": "synthetic-" + secrets.token_hex(16),
                   "note": "Disposable test value; never a user credential"}
    api.success(f"/api/accounts/{account['id']}/credentials", credentials, method="PUT")
    require(api.success(f"/api/accounts/{account['id']}/credentials") == credentials, "Old DPAPI roundtrip failed")
    task = api.success("/api/tasks", {"accountId": account["id"], "name": "Old daily recovery task",
                                    "recurrence": "daily"}, expected_status=201)
    date = api.success("/api/state")["date"]
    api.success(f"/api/tasks/{task['id']}/toggle", {"date": date, "completed": True})
    exported = api.success("/api/export")["data"]
    require(exported["records"], "Old API did not create a completion record")
    return {"accountId": account["id"], "export": exported}, credentials


def verify_materials(database: Path, executable: Path, directory: Path, original: dict) -> tuple[Path, Path, Path, dict]:
    marker = checkpoint(database)
    require(marker is not None, "Candidate did not commit its startup checkpoint")
    require((marker.get("format"), marker.get("checkpoint"), marker.get("state"), marker.get("origin"))
            == (1, "time-contract-v2", "committed", "automatic"), "Wrong startup checkpoint contract")
    recovery = contained(Path(marker["recoveryDirectory"]), directory)
    require(recovery.parent == database.parent / "update-recovery" and recovery.name.startswith("time-contract-v2-"),
            "Unexpected startup recovery location")
    manifest_path = recovery / "recovery.json"
    require(digest(manifest_path) == marker["manifestSha256"], "Checkpoint manifest digest mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    expected = {"format": "leylinebook-startup-recovery", "schemaVersion": 1, "checkpoint": "time-contract-v2",
                "state": "prepared-for-initialization", "automatic": True, "currentDatabase": str(database),
                "sourceDatabase": str(database), "databaseExisted": True, "snapshotIncludesCredentials": True,
                "snapshotContract": "not-inferred", "automaticDatabaseRestore": False,
                "previousExecutableStatus": "saved", "previousExecutableSha256": OLD_SHA256,
                "previousExecutableDownloadUrl": OLD_URL, "newExecutable": str(executable)}
    require(all(manifest.get(key) == value for key, value in expected.items()), "Startup manifest contract mismatch")
    snapshot = contained(Path(manifest["snapshotDatabase"]), recovery)
    saved = contained(Path(manifest["previousExecutableCopy"]), recovery)
    notice = contained(Path(manifest["noticeFile"]), executable.parent)
    require(snapshot.name == "database-before-update.sqlite3", "Unexpected snapshot filename")
    require(digest(snapshot) == manifest["snapshotSha256"], "Snapshot manifest digest mismatch")
    require(digest(saved) == OLD_SHA256, "Recovery executable is not the official old EXE")
    require(logical_database(snapshot) == original, "Full snapshot differs from the real old database")
    require(checkpoint(snapshot) is None, "Snapshot was taken after the candidate checkpoint was written")
    instructions = (recovery / "RECOVERY.txt").read_text(encoding="utf-8-sig")
    require(notice.read_text(encoding="utf-8-sig") == instructions, "Program-side recovery notice differs")
    for fragment in (str(database), str(snapshot), str(saved), OLD_URL, OLD_SHA256,
                     "Do NOT open the current DB", "NEW separate directory", "encrypted credentials"):
        require(fragment in instructions, f"Recovery instructions omit: {fragment}")
    return recovery, snapshot, saved, marker


def run_case(output: Path, old_input: Path, new_input: Path, new_hash: str, case: str, report: dict) -> None:
    directory = contained(output / case, output)
    directory.mkdir()
    data, seed_bin, new_bin = (directory / name for name in ("data", "seed-bin", "candidate-bin"))
    for path in (data, seed_bin, new_bin):
        path.mkdir()
    old = seed_bin / OLD_NAME
    new = new_bin / "LeyLineBook-v3.0.6-Windows-x64.exe"
    copy_new(old_input, old, OLD_SHA256)
    copy_new(new_input, new, new_hash)
    result = {"case": case, "passed": False, "processes": []}
    report["cases"].append(result)
    with headless(old, data, directory / "old-seed", result) as (api, _):
        seed, credentials = seed_old(api)
    database = data / "task_records.db"
    original = logical_database(database)
    require(checkpoint(database) is None, "Old seed unexpectedly contains a candidate checkpoint")
    result["oldDatabase"] = logical_summary(original)
    sibling = new_bin / OLD_NAME
    copy_new(old_input, sibling, OLD_SHA256)
    with headless(new, data, directory / "candidate-first-start", result, automatic=True) as (api, _):
        candidate_export = api.success("/api/export")
        require(candidate_export["appVersion"] == "3.0.6" and candidate_export["schemaVersion"] == 4,
                "Candidate API version/schema is not 3.0.6/schema4")
        require(api.success("/api/state")["timeContractVersion"] == 2, "Candidate time contract is not v2")
        for table in ("accounts", "tasks", "records"):
            require({row["id"] for row in seed["export"][table]}
                    <= {row["id"] for row in candidate_export["data"][table]}, f"Candidate lost old {table}")
        require(api.success(f"/api/accounts/{seed['accountId']}/credentials") == credentials,
                "Candidate did not preserve old DPAPI credentials")
        recovery, snapshot, saved, marker = verify_materials(database, new, directory, original)
        snapshot_hash, saved_hash = digest(snapshot), digest(saved)
        added = api.success("/api/accounts", {"name": "New candidate persistent write", "owner": "Synthetic only"},
                            expected_status=201)
        new_export = api.success("/api/export")["data"]
        require(any(row["id"] == added["id"] for row in new_export["accounts"]), "Candidate write missing")
        require(digest(snapshot) == snapshot_hash and logical_database(snapshot) == original,
                "Candidate write changed the before-start snapshot")
    after_write_files = database_files(database)
    with headless(new, data, directory / "candidate-restart", result, automatic=True) as (api, _):
        require(api.success("/api/export")["data"] == new_export, "Restart restored or lost candidate writes")
        require(checkpoint(database) == marker, "Restart replaced the committed checkpoint")
        require(list((data / "update-recovery").glob("time-contract-v2-*")) == [recovery],
                "Restart created duplicate first-start recovery")
    after_restart = logical_database(database)
    restore = directory / "restore"
    restore.mkdir()
    restore_data, restore_bin = restore / "data", restore / "bin"
    restore_data.mkdir()
    restore_bin.mkdir()
    restored_db, restored_exe = restore_data / "task_records.db", restore_bin / OLD_NAME
    copy_new(snapshot, restored_db, snapshot_hash)
    copy_new(saved, restored_exe, OLD_SHA256)
    require(logical_database(restored_db) == original, "Recovery working copy differs before launch")
    with headless(restored_exe, restore_data, restore / "old-inspection", result) as (api, _):
        exported = api.success("/api/export")
        require(exported["appVersion"] == "3.0.5" and exported["data"] == seed["export"],
                "Restored old EXE did not recover its original exported data")
        require(api.success(f"/api/accounts/{seed['accountId']}/credentials") == credentials,
                "Restored old EXE did not recover original test credentials")
    require(logical_database(database) == after_restart, "Recovery inspection changed the upgraded database")
    require(digest(snapshot) == snapshot_hash and digest(saved) == saved_hash, "Recovery inspection changed source materials")
    require(digest(old) == OLD_SHA256 and digest(sibling) == OLD_SHA256 and digest(new) == new_hash,
            "An isolated program copy changed")
    result.update({"passed": True, "snapshot": str(snapshot), "recovery": str(recovery),
                   "savedOldExecutable": str(saved), "checkpoint": marker, "snapshotSha256": snapshot_hash,
                   "fullSnapshotExact": True, "syntheticCredentialsRecovered": True,
                   "newWritesPreserved": True, "afterWriteDatabaseFiles": after_write_files,
                   "restoredDatabase": str(restored_db), "upgradedDatabase": logical_summary(after_restart)})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-exe", type=Path, default=ROOT / OLD_NAME)
    parser.add_argument("--new-exe", type=Path, required=True)
    parser.add_argument("--new-sha256", required=True)
    args = parser.parse_args()
    require(os.name == "nt", "Windows required; no cases executed")
    require(re.fullmatch(r"[0-9a-fA-F]{64}", args.new_sha256) is not None, "Candidate SHA-256 is required")
    old, new = args.old_exe.resolve(strict=True), args.new_exe.resolve(strict=True)
    new_hash = args.new_sha256.lower()
    require(old != new and old.suffix.lower() == new.suffix.lower() == ".exe", "Two distinct EXE inputs are required")
    require(digest(old) == OLD_SHA256 and old.stat().st_size == 16154324, "Official old EXE identity mismatch")
    require(digest(new) == new_hash and new_hash != OLD_SHA256, "Candidate digest mismatch")
    require(OUTPUT.resolve() == OUTPUT, "Output root must not redirect")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="real-headless-upgrade-", dir=OUTPUT))
    report = {"level": "real-dual-frozen-exe-headless-first-start-and-isolated-recovery", "passed": False,
              "completeAutomaticUpgrade": False, "oldCoordinatorExecuted": False, "nativeReadyVerified": False,
              "githubDiscoveryDownload": False, "fakeReady": False, "launchMode": "--no-browser-only",
              "frozenFailureCasesExecuted": False,
              "oldInput": str(old), "oldSha256": OLD_SHA256, "newInput": str(new), "newSha256": new_hash,
              "output": str(output), "cases": []}
    print(f"Headless dual-EXE evidence: {output}", flush=True)
    try:
        run_case(output, old, new, new_hash, "success", report)
        print(json.dumps({"case": "success", "passed": True}), flush=True)
        report["passed"] = True
    except Exception as error:
        report["error"] = repr(error)
        report["traceback"] = traceback.format_exc()
    finally:
        try:
            require(digest(old) == OLD_SHA256 and digest(new) == new_hash, "Input executable changed")
        except Exception as error:
            report["passed"] = False
            report["inputVerificationError"] = repr(error)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=True, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
