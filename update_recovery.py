"""Preserve recovery evidence before allowing upgrade initialization or replacement."""

from __future__ import annotations

import json
import base64
import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath


STARTUP_CHECKPOINT_KEY = "startup_time_contract_v2"
PREVIOUS_EXE_NAME = "LeyLineBook-v3.0.5-Windows-x64.exe"
PREVIOUS_EXE_SHA256 = "590e1f54a898341c6ab1a1716505d51552aef6ec582afcace8e172b9aa4f8b95"
PREVIOUS_EXE_URL = (
    "https://github.com/shuxiachai/LeyLineBook/releases/download/v3.0.5/" + PREVIOUS_EXE_NAME
)
MANUAL_RECOVERY_HELP = (
    "\u9996\u6b21\u81ea\u52a8\u4fdd\u62a4\u4ec5\u652f\u6301\u5b98\u65b9 v3.0.5 \u7684\u56fa\u5b9a\u6587\u4ef6\u540d\u548c SHA-256\u3002\u8f83\u65e9\u7248\u672c\u6216\u91cd\u547d\u540d\u65e7\u7a0b\u5e8f\u4e0d\u652f\u6301\u6b64\u81ea\u52a8\u8def\u5f84\u3002 "
    "\u8bf7\u5148\u9000\u51fa\u6240\u6709\u5b9e\u4f8b\uff0c\u4fdd\u5168\u539f EXE \u548c\u5b8c\u6574\u6570\u636e\u76ee\u5f55\uff08\u5305\u62ec -wal/-shm/-journal\uff09\u3002\u4e0d\u8981\u5220\u9664\u5f02\u5e38 journal\uff0c\u4e5f\u4e0d\u8981\u8ba9\u65e7\u7a0b\u5e8f\u6253\u5f00\u5f53\u524d\u6570\u636e\u5e93\u3002 "
    "\u624b\u52a8\u66ff\u6362\u65f6\uff0c\u5c06\u5019\u9009\u7a0b\u5e8f\u4ee5 3.0.6 \u6587\u4ef6\u540d\u653e\u5165\u72ec\u7acb\u7a0b\u5e8f\u6587\u4ef6\u5939\uff0c\u518d\u4e0d\u5e26 --update-health-file \u624b\u52a8\u542f\u52a8\uff1b\u7a0b\u5e8f\u4ecd\u4f1a\u5148\u4fdd\u62a4\u5df2\u6709\u6570\u636e\uff0c\u4e0d\u4f1a\u81ea\u52a8\u964d\u7ea7\u91cd\u8bd5\u3002 "
    "\u82e5\u5b58\u5728\u5f02\u5e38 rollback journal\uff0c\u8bf7\u4fdd\u7559\u539f\u4ef6\uff0c\u53ea\u5728\u5b8c\u6574\u9694\u79bb\u526f\u672c\u4e2d\u4f7f\u7528\u5339\u914d\u65e7\u7248\u672c\u6062\u590d\u5e76\u9a8c\u8bc1\uff0c\u6216\u5bfb\u6c42\u652f\u6301\u3002\u6062\u590d\u65e7\u6570\u636e\u65f6\u5fc5\u987b\u663e\u5f0f\u8bbe\u7f6e\u72ec\u7acb LEYLINEBOOK_DATA_DIR\u3002 "
)


def _absolute_path(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError(f"Recovery path must be absolute and must not redirect: {path}")
    return path


def _regular_file(value: Path) -> Path:
    path = _absolute_path(value)
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError(f"Recovery path must be a regular file without hard links: {path}")
    return path


def _sqlite_paths(database: Path) -> Path:
    for suffix in ("", "-wal", "-shm", "-journal"):
        _regular_file(database.with_name(database.name + suffix))
    journal = database.with_name(database.name + "-journal")
    if journal.exists() and journal.stat().st_size:
        raise RuntimeError(
            f"Nonempty rollback journal detected; no SQLite connection was opened. Preserve: {journal}. "
            + MANUAL_RECOVERY_HELP
        )
    return database


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with _regular_file(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_exclusive(source: Path, target: Path) -> None:
    with _regular_file(source).open("rb") as src, _regular_file(target).open("xb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    if _sha256(source) != _sha256(target):
        raise RuntimeError("Recovery copy failed SHA-256 verification")


def _write_exclusive(path: Path, text: str) -> None:
    with _regular_file(path).open("x", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())


def _snapshot_database(database: Path, snapshot: Path) -> None:
    database, snapshot = _sqlite_paths(database), _regular_file(snapshot)
    with snapshot.open("xb"):
        pass
    deadline = time.monotonic() + 30

    def check_deadline(status: int, remaining: int, total: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError("Database snapshot timed out; startup/update must not continue")

    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(snapshot)) as target:
            source.backup(target, pages=256, progress=check_deadline, sleep=0.05)
            target.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            target.execute("PRAGMA journal_mode=DELETE")
            if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("Recovery snapshot failed SQLite integrity_check")


@contextmanager
def _startup_lock(database: Path):
    lock_path = _regular_file(database.with_name(f".{database.name}.startup.lock"))
    with lock_path.open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("Database startup already in progress; no initialization attempted") from error
        yield


@contextmanager
def _startup_connection(database: Path):
    _sqlite_paths(database)
    with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=OFF")
        # Reserve the writer before taking a snapshot through a separate reader.
        # The same writer owns initialization and checkpoint commit, without a gap.
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        finally:
            connection.rollback()


def _read_checkpoint(connection: sqlite3.Connection) -> dict | None:
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_meta'").fetchone():
        return None
    row = connection.execute("SELECT value FROM app_meta WHERE key=?", (STARTUP_CHECKPOINT_KEY,)).fetchone()
    if row is None:
        return None
    marker = json.loads(row[0])
    fields = {"format", "checkpoint", "state", "origin", "recoveryDirectory", "manifestSha256"}
    if not isinstance(marker, dict) or not fields.issubset(marker) or (
        type(marker.get("format")) is not int or marker["format"] != 1
        or marker["checkpoint"] != "time-contract-v2" or marker["state"] != "committed"
        or marker["origin"] not in ("fresh", "manual", "automatic", "legacy-copy")
    ):
        raise ValueError("Invalid startup checkpoint; database was not initialized")
    directory, digest = marker["recoveryDirectory"], marker["manifestSha256"]
    if marker["origin"] == "fresh":
        valid_evidence = directory is None and digest is None
    else:
        # Historical paths are syntax only: do not resolve or access moved/deleted evidence.
        valid_path = False
        if isinstance(directory, str) and not any(ord(char) < 32 for char in directory):
            candidates = (PureWindowsPath(directory), PurePosixPath(directory))
            valid_path = any(path.is_absolute() and ".." not in path.parts for path in candidates)
        valid_evidence = valid_path and isinstance(digest, str) and re.fullmatch("[0-9a-f]{64}", digest) is not None
    if not valid_evidence:
        raise ValueError("Invalid startup checkpoint evidence fields; database was not initialized")
    # Historical evidence can be moved or cleaned after a committed checkpoint.
    # A marker never proves UI readiness, nor compatibility with an older program.
    return marker


def _prepare_startup_checkpoint(
    database: Path, source_database: Path | None, current_exe: Path | None, automatic: bool,
) -> Path:
    root = _absolute_path(database.parent / "update-recovery")
    root.mkdir(exist_ok=True)
    directory = _absolute_path(Path(tempfile.mkdtemp(prefix="time-contract-v2-", dir=root)))
    try:
        previous = _regular_file(current_exe.parent / PREVIOUS_EXE_NAME) if current_exe else None
        saved = None
        if previous is not None and previous == current_exe:
            previous = None  # A candidate replacing the old filename is not an old executable.
        if previous is not None and previous.exists():
            if _sha256(previous) != PREVIOUS_EXE_SHA256:
                raise RuntimeError(
                    f"Previous executable SHA-256 mismatch: {previous}. Required: {PREVIOUS_EXE_SHA256}"
                )
            saved = directory / "previous-v3.0.5.exe.saved"
            _copy_exclusive(previous, saved)
            if _sha256(saved) != PREVIOUS_EXE_SHA256:
                raise RuntimeError("Saved previous executable failed SHA-256 verification")
        elif automatic:
            raise FileNotFoundError(
                f"Automatic first upgrade requires a separate official {PREVIOUS_EXE_NAME} beside the new program. "
                f"Required SHA-256: {PREVIOUS_EXE_SHA256}"
            )
        snapshot = directory / "database-before-update.sqlite3" if source_database else None
        if snapshot is not None:
            _snapshot_database(source_database, snapshot)
        notice_root = current_exe.parent if current_exe else database.parent
        notice = _absolute_path(notice_root / f"LeyLineBook-update-recovery-{directory.name}.txt")
        metadata = {
            "format": "leylinebook-startup-recovery", "schemaVersion": 1,
            "checkpoint": "time-contract-v2", "state": "prepared-for-initialization",
            "createdUtc": datetime.now(timezone.utc).isoformat(), "automatic": automatic,
            "currentDatabase": str(database),
            "sourceDatabase": str(source_database) if source_database else None,
            "databaseExisted": source_database is not None,
            "snapshotDatabase": str(snapshot) if snapshot else None,
            "snapshotSha256": _sha256(snapshot) if snapshot else None,
            "snapshotIncludesCredentials": snapshot is not None,
            "snapshotContract": "not-inferred", "automaticDatabaseRestore": False,
            "previousExecutableStatus": "saved" if saved else "download-required",
            "previousExecutableCopy": str(saved) if saved else None,
            "previousExecutableSha256": PREVIOUS_EXE_SHA256,
            "previousExecutableDownloadUrl": PREVIOUS_EXE_URL,
            "newExecutable": str(current_exe) if current_exe else None,
            "noticeFile": str(notice),
        }
        instructions = (
            "LeyLineBook TIME-CONTRACT 2 STARTUP RECOVERY\n\n"
            f"Current database: {database}\n"
            f"Before-initialization snapshot: {snapshot or 'No previous database existed'}\n"
            f"Saved previous executable: {saved or 'NOT SAVED; download required'}\n"
            f"Official v3.0.5 download: {PREVIOUS_EXE_URL}\n"
            f"Required executable SHA-256: {PREVIOUS_EXE_SHA256}\n"
            f"Recovery evidence: {directory}\n\n"
            "These materials were prepared before initialization. They do not prove UI readiness.\n"
            "No database was automatically restored. Do NOT open the current DB with the old program.\n"
            "Close all app processes first. Preserve the entire current data directory, including WAL/SHM files.\n"
            "Snapshot time-contract compatibility is NOT inferred, especially after manual program/database replacement.\n"
            "Only for data confirmed to come from v3.0.5: copy the snapshot as task_records.db to a NEW separate directory.\n"
            "Copy the saved program (or verify the official download digest), renaming only that separate copy to .exe.\n"
            "Before launching it, explicitly set LEYLINEBOOK_DATA_DIR to the NEW directory. Never run it against the current DB.\n"
            "Do not overwrite the current DB. Keep both versions and seek assistance if compatibility/new writes are uncertain.\n"
            "The snapshot includes encrypted credentials; do not upload it. Decryption requires the original Windows user/machine.\n"
            "After successful initialization, historical recovery files are not required for normal startup.\n"
        )
        _write_exclusive(directory / "RECOVERY.txt", instructions)
        _write_exclusive(notice, instructions)
        _write_exclusive(directory / "recovery.json", json.dumps(metadata, ensure_ascii=True, indent=2) + "\n")
        return directory
    except Exception as error:
        detail = f"First-start recovery preparation failed: {error}\nEvidence: {directory}\n{MANUAL_RECOVERY_HELP}\n"
        for path in (
            directory / "PREPARATION-FAILED.txt",
            (current_exe.parent if current_exe else database.parent)
            / f"LeyLineBook-update-recovery-failed-{directory.name}.txt",
        ):
            try:
                _write_exclusive(path, detail)
            except Exception:
                pass  # Keep the original failure actionable even when diagnostic writes fail.
        raise RuntimeError(detail) from error


@contextmanager
def startup_recovery(
    db_path: Path, *, current_exe: Path | None = None,
    legacy_db: Path | None = None, automatic: bool = False,
):
    """Yield the startup transaction only after the one-time checkpoint is prepared."""
    database = _sqlite_paths(Path(db_path))
    program = _regular_file(current_exe) if current_exe is not None else None
    if automatic and program is None:
        raise ValueError("Automatic startup recovery requires a frozen executable")
    database.parent.mkdir(parents=True, exist_ok=True)
    recovery = None
    with _startup_lock(database):
        try:
            existed = database.exists()
            legacy = _regular_file(legacy_db) if legacy_db is not None and not existed else None
            if not existed:
                if any(database.with_name(database.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                    raise RuntimeError("Database is missing but SQLite sidecars exist; refusing to create or replace data")
                if legacy is not None and legacy.exists():
                    with _startup_connection(legacy):
                        recovery = _prepare_startup_checkpoint(database, legacy, program, automatic)
                        _copy_exclusive(recovery / "database-before-update.sqlite3", database)
                else:
                    # Even without a DB, an old automatic updater will delete its EXE
                    # after readiness. Preserve that program before creating anything.
                    if automatic:
                        recovery = _prepare_startup_checkpoint(database, None, program, True)
                    with database.open("xb"):
                        pass
            with _startup_connection(database) as connection:
                marker = _read_checkpoint(connection)
                if recovery is not None or marker is None:
                    if recovery is None and existed:
                        recovery = _prepare_startup_checkpoint(database, database, program, automatic)
                    marker = {
                        "format": 1, "checkpoint": "time-contract-v2", "state": "committed",
                        "origin": "legacy-copy" if legacy is not None and legacy.exists() else
                                  "automatic" if automatic else "manual" if existed else "fresh",
                        "recoveryDirectory": str(recovery) if recovery else None,
                        "manifestSha256": _sha256(recovery / "recovery.json") if recovery else None,
                    }
                yield connection
                if connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Startup foreign key check failed; initialization was rolled back")
                connection.execute("CREATE TABLE IF NOT EXISTS app_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute(
                    "INSERT OR REPLACE INTO app_meta(key, value) VALUES(?, ?)",
                    (STARTUP_CHECKPOINT_KEY, json.dumps(marker, ensure_ascii=True)),
                )
                connection.commit()
        except Exception as error:
            raise RuntimeError(
                f"First-start recovery failed; startup stopped before readiness. Evidence: {recovery or database.parent}. "
                f"{error}\n{MANUAL_RECOVERY_HELP}"
            ) from error


def build_update_batch(
    temp_exe: Path, new_exe: Path, current_exe: Path, health_file: Path,
    recovery_dir: Path | None = None,
) -> str:
    """Build the detached coordinator; preserve old/new programs and both DBs."""
    def literal(value: Path) -> str:
        text = str(Path(value).resolve())
        if any(character in text for character in ('\x00', '\r', '\n', '"')):
            raise ValueError("Invalid update path")
        return "'" + text.replace("'", "''") + "'"

    marker_root = Path(recovery_dir) if recovery_dir is not None else Path(new_exe).parent
    stem = "update" if recovery_dir is not None else f"LeyLineBook-update-failure-{Path(health_file).stem}"
    failed = marker_root / ("update-failed.txt" if recovery_dir is not None else stem + ".txt")
    succeeded = marker_root / ("update-succeeded.txt" if recovery_dir is not None else stem + "-succeeded.txt")
    script = f"""
$ErrorActionPreference = 'Stop'
$tempExe = {literal(temp_exe)}
$newExe = {literal(new_exe)}
$oldExe = {literal(current_exe)}
$health = {literal(health_file)}
$failed = {literal(failed)}
$succeeded = {literal(succeeded)}
$recovery = {literal(marker_root)}
function Record-Result($path, $reason, $detail) {{
    $text = "reason: $reason`r`ndetail: $detail`r`nrecovery: $recovery`r`nprevious executable: $oldExe`r`nnew executable: $newExe`r`nhealth marker: $health`r`nNo files or databases were automatically restored or deleted. Read RECOVERY.txt before using the old program.`r`n"
    [IO.File]::WriteAllText($path, $text, [Text.UTF8Encoding]::new($true))
}}
Start-Sleep -Seconds 2
try {{
    if ($oldExe -eq $newExe) {{ throw 'New path equals the old program; refusing replacement' }}
    if (Test-Path -LiteralPath $newExe) {{ throw 'Destination already exists; refusing replacement' }}
    if (Test-Path -LiteralPath $health) {{ throw 'Health marker already exists; refusing stale readiness' }}
    Move-Item -LiteralPath $tempExe -Destination $newExe -ErrorAction Stop
}} catch {{
    Record-Result $failed 'move_failed' $_.Exception.Message
    exit 1
}}
try {{
    $process = Start-Process -FilePath $newExe -ArgumentList @('--update-health-file', ('"' + $health + '"')) -PassThru -ErrorAction Stop
}} catch {{
    Record-Result $failed 'start_failed' $_.Exception.Message
    exit 1
}}
$watch = [Diagnostics.Stopwatch]::StartNew()
while ($watch.Elapsed.TotalSeconds -lt 30) {{
    $process.Refresh()
    if ($process.HasExited) {{
        Record-Result $failed 'start_failed' ('New process exited before readiness: ' + $process.ExitCode)
        exit 1
    }}
    if (Test-Path -LiteralPath $health) {{
        Record-Result $succeeded 'healthy' 'UI readiness received before the deadline; recovery files retained'
        exit 0
    }}
    Start-Sleep -Milliseconds 200
}}
Record-Result $failed 'health_timeout' 'No UI readiness within 30 seconds. The new process may still be running; do not start the old program against the current database.'
exit 1
"""
    # Encode only our generated script to preserve Unicode, spaces, %, quotes and
    # shell metacharacters across cmd.exe. No execution-policy override is needed.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    chunks = [encoded[offset:offset + 4000] for offset in range(0, len(encoded), 4000)]
    assignments = [f'set "LLB_UPDATE_{index}={chunk}"' for index, chunk in enumerate(chunks)]
    expression = " + ".join(f"$env:LLB_UPDATE_{index}" for index in range(len(chunks)))
    command = f"$code = {expression}; & ([ScriptBlock]::Create([Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($code))))"
    return "\r\n".join([
        "@echo off", "setlocal DisableDelayedExpansion", *assignments,
        f'powershell.exe -NoProfile -NonInteractive -Command "{command}"',
        "exit /b %errorlevel%", "",
    ])


def prepare_update_recovery(db_path: Path, current_exe: Path, new_exe: Path) -> Path:
    """Return a recovery directory, or raise before the updater may replace files."""
    db_path = Path(db_path).resolve(strict=True)
    current_exe = Path(current_exe).resolve(strict=True)
    new_exe = Path(new_exe).resolve()
    root = db_path.parent / "update-recovery"
    root.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
    directory = Path(tempfile.mkdtemp(prefix=stamp, dir=root))
    snapshot = directory / "database-before-update.sqlite3"
    deadline = time.monotonic() + 30

    def check_deadline(status: int, remaining: int, total: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError("Database snapshot timed out; update must not continue")

    try:
        # SQLite's backup API includes committed WAL data and every table, including
        # encrypted credentials. JSON exports deliberately do not have this scope.
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(snapshot)) as target:
                source.backup(target, pages=256, progress=check_deadline, sleep=0.05)
                if target.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise RuntimeError("Upgrade snapshot failed SQLite quick_check")
        metadata = {
            "format": "leylinebook-update-recovery",
            "schemaVersion": 1,
            "createdUtc": datetime.now(timezone.utc).isoformat(),
            "currentDatabase": str(db_path),
            "snapshotDatabase": str(snapshot),
            "previousExecutable": str(current_exe),
            "newExecutable": str(new_exe),
            "automaticDatabaseRestore": False,
            "snapshotIncludesCredentials": True,
        }
        (directory / "recovery.json").write_text(
            json.dumps(metadata, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
        )
        instructions = (
            "LeyLineBook UPDATE RECOVERY / \u5730\u8109\u7c3f\u5347\u7ea7\u6062\u590d\n\n"
            f"\u5f53\u524d\u6570\u636e\u5e93 / Current database:\n{db_path}\n\n"
            f"\u5347\u7ea7\u524d\u5b8c\u6574\u5feb\u7167 / Before-update snapshot:\n{snapshot}\n\n"
            f"\u65e7\u7a0b\u5e8f / Previous executable:\n{current_exe}\n\n"
            f"\u65b0\u7a0b\u5e8f / New executable:\n{new_exe}\n\n"
            "\u672a\u81ea\u52a8\u56de\u6eda\u6570\u636e\u5e93\u3002\u5f53\u524d\u6570\u636e\u5e93\u53ef\u80fd\u5df2\u6709\u5347\u7ea7\u6216\u65b0\u5199\u5165\uff0c\u4e0d\u8981\u76f4\u63a5\u7528\u65e7\u7a0b\u5e8f\u6253\u5f00\u5b83\u3002\n"
            "No database was automatically restored. Do NOT open the current DB with the old program.\n"
            "\u5982\u5347\u7ea7\u5931\u8d25\uff1a\u5148\u5173\u95ed\u6240\u6709\u5730\u8109\u7c3f\u7a97\u53e3\u53ca\u8fdb\u7a0b\uff0c\u590d\u5236\u4fdd\u7559\u5f53\u524d\u6570\u636e\u76ee\u5f55\u7684\u5168\u90e8\u5185\u5bb9\uff08\u542b\u53ef\u80fd\u7684 -wal/-shm \u6587\u4ef6\uff09\u548c\u672c\u6062\u590d\u76ee\u5f55\u3002\n"
            "On failure: close all app processes, then preserve the entire current data directory (including WAL/SHM files) and this recovery directory.\n"
            "\u5982\u9700\u67e5\u770b\u5347\u7ea7\u524d\u6570\u636e\uff0c\u628a\u5feb\u7167\u590d\u5236\u5230\u5168\u65b0\u7684\u72ec\u7acb\u6587\u4ef6\u5939\u5e76\u547d\u540d\u4e3a task_records.db\uff0c\u4ec5\u5728\u660e\u786e\u8bbe\u7f6e LEYLINEBOOK_DATA_DIR \u6307\u5411\u8be5\u6587\u4ef6\u5939\u540e\u542f\u52a8\u65e7\u7a0b\u5e8f\u3002\n"
            "To inspect old data, copy the snapshot as task_records.db in a NEW separate directory; launch the old program only with LEYLINEBOOK_DATA_DIR set to that directory.\n"
            "This isolation requires a version supporting LEYLINEBOOK_DATA_DIR (3.0.5 or later). Do not assume older versions honor it.\n"
            "\u4e0d\u8981\u7528\u5feb\u7167\u8986\u76d6\u5f53\u524d\u6570\u636e\u5e93\uff1b\u65e0\u6cd5\u786e\u8ba4\u65b0\u5199\u5165\u65f6\u4fdd\u7559\u4e24\u4efd\u6570\u636e\u5e76\u5bfb\u6c42\u652f\u6301\u3002\n"
            "Do not overwrite the current DB with the snapshot. Retain both and seek assistance when new writes are uncertain.\n"
            "\u5feb\u7167\u542b\u654f\u611f\u51ed\u636e\uff0c\u8bf7\u52ff\u516c\u5f00\u4e0a\u4f20\u3002\u51ed\u636e\u89e3\u5bc6\u4ecd\u9700\u539f Windows \u8d26\u6237/\u673a\u5668\u3002\n"
            "The snapshot contains sensitive credentials. Do not upload it publicly; decryption still requires the original Windows user/machine.\n"
        )
        (directory / "RECOVERY.txt").write_text(instructions, encoding="utf-8-sig")
        # A unique notice next to the program is discoverable even when APPDATA is hidden.
        notice = current_exe.parent / f"LeyLineBook-update-recovery-{directory.name}.txt"
        with notice.open("x", encoding="utf-8-sig") as output:
            output.write(instructions)
        return directory
    except Exception as error:
        raise RuntimeError(f"Update recovery preparation failed; do not update. Evidence: {directory}") from error
