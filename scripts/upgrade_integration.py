"""Run Windows updater coordination against a compiled synthetic process fixture.

This does not test the LeyLineBook UI, download service, or native WebView2.
All artifacts are retained under output/playwright for inspection.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from update_recovery import build_update_batch, prepare_update_recovery


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def wait_for(path: Path, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"Missing fixture evidence: {path}")
        time.sleep(0.05)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_case(output: Path, fixture: Path, case: str) -> dict:
    directory = output / (case + " spaces % ! & ' \u6d4b\u8bd5")
    directory.mkdir()
    old = directory / "old % ! & ' \u6d4b\u8bd5.exe"
    new = directory / "new % ! & ' \u6d4b\u8bd5.exe"
    staged = directory / "staged update.part"
    shutil.copyfile(fixture, old)
    if case != "move_failure":
        if case == "start_failure":
            staged.write_bytes(b"invalid synthetic executable")
        else:
            shutil.copyfile(fixture, staged)
    database = directory / "task_records.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE credentials(id INTEGER, ciphertext BLOB)")
        connection.execute("INSERT INTO credentials VALUES(1, ?)", (b"synthetic-secret-only",))
        connection.commit()
    recovery = prepare_update_recovery(database, old, new)
    original_db, original_exe = digest(database), digest(old)
    health = directory / ("LeyLineBook-update-health-" + secrets.token_hex(8) + ".ok")
    batch = directory / "coordinator.cmd"
    batch.write_text(build_update_batch(staged, new, old, health, recovery), encoding="ascii")
    env = {
        **os.environ, "LEYLINEBOOK_UPGRADE_FIXTURE_DIR": str(directory),
        "LEYLINEBOOK_UPGRADE_FIXTURE_MODE": case,
        "LEYLINEBOOK_DATA_DIR": str(directory), "TEMP": str(directory), "TMP": str(directory),
    }
    process = None
    started = time.monotonic()
    try:
        with (directory / "coordinator.log").open("wb") as log:
            process = subprocess.Popen(
                ["cmd.exe", "/d", "/c", batch.name], cwd=directory, env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.DETACHED_PROCESS,
            )
            code = process.wait(timeout=50)
        elapsed = time.monotonic() - started
        require(elapsed >= 2, "Detached coordinator skipped its initial wait")
        success = case == "healthy"
        require(code == (0 if success else 1), f"Unexpected coordinator exit: {code}")
        marker = recovery / ("update-succeeded.txt" if success else "update-failed.txt")
        require(marker.is_file(), "Coordinator did not create its real result file")
        reason = {"healthy": "healthy", "move_failure": "move_failed", "start_failure": "start_failed", "startup_failure": "start_failed", "timeout": "health_timeout", "late": "health_timeout"}[case]
        text = marker.read_text(encoding="utf-8-sig")
        require(f"reason: {reason}" in text, f"Wrong failure classification: {text}")
        require(str(recovery) in text, "Failure marker lost its recovery path")
        if case in {"timeout", "late"}:
            require(elapsed >= 32, f"Health timeout returned too early: {elapsed:.3f}s")
        if case == "late":
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("INSERT INTO credentials VALUES(2, ?)", (b"new-write-after-timeout",))
                connection.commit()
            after_write = digest(database)
            wait_for(health, timeout=10)
            require(marker.read_text(encoding="utf-8-sig") == text, "Late readiness changed the failure decision")
            require(not (recovery / "update-succeeded.txt").exists(), "Late readiness was incorrectly accepted")
            require(digest(database) == after_write, "Late readiness restored or changed new data")
        else:
            require(digest(database) == original_db, "Coordinator changed the current test database")
        require(old.is_file() and digest(old) == original_exe, "Old program was changed or deleted")
        snapshot = recovery / "database-before-update.sqlite3"
        with closing(sqlite3.connect(snapshot)) as connection:
            require(connection.execute("PRAGMA quick_check").fetchall() == [("ok",)], "Snapshot failed quick_check")
            require(connection.execute("SELECT * FROM credentials").fetchall() == [(1, b"synthetic-secret-only")], "Snapshot lost credentials or was overwritten")
        return {"case": case, "exitCode": code, "seconds": round(elapsed, 3), "reason": reason, "marker": str(marker), "database": str(database), "snapshot": str(snapshot)}
    finally:
        (directory / "fixture-stop.txt").write_text("stop", encoding="ascii")
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        pid_file = directory / "fixture-pid.txt"
        if pid_file.exists():
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x00100000, False, int(pid_file.read_text()))
            if handle:
                try:
                    require(kernel.WaitForSingleObject(handle, 40000) == 0, "Owned fixture did not exit after stop signal")
                finally:
                    kernel.CloseHandle(handle)


def main() -> int:
    if os.name != "nt":
        print("Windows is required; no cases were executed.", file=sys.stderr)
        return 2
    parent = (ROOT / "output/playwright").resolve()
    parent.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="upgrade-fixture-", dir=parent))
    require(output.parent == parent, "Unexpected fixture output path")
    print(f"Fixture evidence: {output}", flush=True)
    report = {"level": "synthetic-windows-process-coordination", "realApplicationUpgrade": False, "nativeWebView2": False, "cases": []}
    try:
        compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        require(compiler.is_file(), "The Windows .NET Framework C# compiler is unavailable")
        fixture = output / "SyntheticUpdateFixture.exe"
        result = subprocess.run([str(compiler), "/nologo", "/target:winexe", "/out:" + str(fixture), str(ROOT / "scripts/fixtures/update_process.cs")], capture_output=True, timeout=30)
        (output / "build.log").write_bytes(result.stdout + result.stderr)
        require(result.returncode == 0 and fixture.exists(), "Synthetic fixture compilation failed")
        started = time.monotonic()
        legacy = subprocess.run(["cmd.exe", "/d", "/c", "timeout /t 2 /nobreak > nul"], stdin=subprocess.DEVNULL, capture_output=True, creationflags=subprocess.DETACHED_PROCESS, timeout=10)
        report["legacyTimeoutProbe"] = {"exitCode": legacy.returncode, "seconds": round(time.monotonic() - started, 3), "stderr": legacy.stderr.decode(errors="replace")}
        for case in ["healthy", "move_failure", "start_failure", "startup_failure", "timeout", "late"]:
            result = run_case(output, fixture, case)
            report["cases"].append(result)
            print(json.dumps(result, ensure_ascii=True), flush=True)
        report["passed"] = True
        return 0
    except Exception as error:
        report["passed"] = False
        report["error"] = repr(error)
        raise
    finally:
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
