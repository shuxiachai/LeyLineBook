import hashlib
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import app
import update_recovery as recovery


def _hold_startup(database, ready, release, errors):
    try:
        with recovery.startup_recovery(Path(database)):
            ready.set()
            if not release.wait(15):
                raise TimeoutError("Isolated test did not release its startup transaction")
    except Exception as error:
        errors.put(repr(error))
        raise


class FirstUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="leylinebook-first-upgrade-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / "task_records.db"
        self.program_dir = self.root / "programs"
        self.program_dir.mkdir()
        self.program = self.program_dir / "LeyLineBook-v3.0.6-Windows-x64.exe"
        self.program.write_bytes(b"synthetic new program; never executed")
        self.old_program = self.program_dir / recovery.PREVIOUS_EXE_NAME
        self.old_bytes = b"synthetic old program; never executed"
        self.old_program.write_bytes(self.old_bytes)
        self.hash_patch = patch.object(recovery, "PREVIOUS_EXE_SHA256", hashlib.sha256(self.old_bytes).hexdigest())
        self.hash_patch.start()
        self.addCleanup(self.hash_patch.stop)
        self.db_patch = patch.object(app, "DB_PATH", self.database)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)

    def seed(self, database=None, payload=b"encrypted-test-credentials"):
        database = database or self.database
        self.assertTrue(database.is_absolute() and database.is_relative_to(self.root))
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE private_payload(id INTEGER PRIMARY KEY, ciphertext BLOB)")
            connection.execute("INSERT INTO private_payload VALUES(1, ?)", (payload,))
            connection.commit()

    def checkpoint(self):
        with closing(sqlite3.connect(self.database)) as connection:
            value = connection.execute(
                "SELECT value FROM app_meta WHERE key=?", (recovery.STARTUP_CHECKPOINT_KEY,)
            ).fetchone()[0]
            return json.loads(value)

    def manifest(self):
        directory = Path(self.checkpoint()["recoveryDirectory"])
        self.assertTrue(directory.is_absolute() and directory.is_relative_to(self.root))
        manifest = directory / "recovery.json"
        self.assertFalse(manifest.read_bytes().startswith(b"\xef\xbb\xbf"))
        return directory, json.loads(manifest.read_text(encoding="utf-8"))

    def assert_uninitialized(self, original):
        self.assertEqual(self.database.read_bytes(), original)
        self.assertFalse(any(self.root.glob("update-recovery/*/recovery.json")))

    def health(self):
        return self.root / "LeyLineBook-update-health-0123456789abcdef.ok"

    def test_automatic_wal_snapshot_precedes_writes_and_keeps_saved_program(self):
        self.seed()
        with closing(sqlite3.connect(self.database)) as source:
            source.execute("PRAGMA journal_mode=WAL")
            source.execute("INSERT INTO private_payload VALUES(2, ?)", (b"committed-in-wal",))
            source.commit()
            with recovery.startup_recovery(self.database, current_exe=self.program, automatic=True) as connection:
                directory = next((self.root / "update-recovery").iterdir())
                manifest = json.loads((directory / "recovery.json").read_text(encoding="utf-8-sig"))
                self.assertEqual(manifest["state"], "prepared-for-initialization")
                self.assertTrue(Path(manifest["noticeFile"]).exists())
                with closing(sqlite3.connect(manifest["snapshotDatabase"])) as snapshot:
                    self.assertEqual(snapshot.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                    self.assertEqual(snapshot.execute("SELECT ciphertext FROM private_payload ORDER BY id").fetchall(),
                                     [(b"encrypted-test-credentials",), (b"committed-in-wal",)])
                with closing(sqlite3.connect(self.database, timeout=0.05)) as contender:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                        contender.execute("INSERT INTO private_payload VALUES(99, NULL)")
                connection.execute("INSERT INTO private_payload VALUES(3, ?)", (b"new-contract-write",))
            self.assertEqual(source.execute("SELECT count(*) FROM private_payload").fetchone(), (3,))
        directory, manifest = self.manifest()
        saved = Path(manifest["previousExecutableCopy"])
        self.assertEqual(saved.suffix, ".saved")
        self.assertEqual(saved.read_bytes(), self.old_bytes)
        self.assertEqual(manifest["snapshotSha256"], recovery._sha256(Path(manifest["snapshotDatabase"])))
        self.assertEqual(self.checkpoint()["manifestSha256"], recovery._sha256(directory / "recovery.json"))
        self.assertTrue(self.old_program.is_relative_to(self.root))
        self.old_program.unlink()  # The released updater deletes only its original EXE.
        self.assertTrue(saved.exists())
        self.assertFalse(manifest["automaticDatabaseRestore"])
        self.assertEqual(manifest["snapshotContract"], "not-inferred")

    @unittest.skipUnless(os.name == "nt", "DPAPI is a Windows credential format")
    def test_snapshot_keeps_actual_synthetic_dpapi_ciphertext(self):
        ciphertext = app.dpapi_protect("isolated first-upgrade test credential")
        self.seed(payload=ciphertext)
        with recovery.startup_recovery(self.database):
            pass
        _, manifest = self.manifest()
        with closing(sqlite3.connect(manifest["snapshotDatabase"])) as snapshot:
            saved = snapshot.execute("SELECT ciphertext FROM private_payload").fetchone()[0]
        self.assertEqual(saved, ciphertext)
        self.assertEqual(app.dpapi_unprotect(saved), "isolated first-upgrade test credential")

    def test_missing_old_program_automatic_fails_without_writing_database(self):
        self.seed()
        original = self.database.read_bytes()
        self.old_program.unlink()
        with self.assertRaisesRegex(RuntimeError, "stopped before readiness"):
            with recovery.startup_recovery(self.database, current_exe=self.program, automatic=True):
                self.fail("Initialization was entered without a saved old executable")
        self.assert_uninitialized(original)

    def test_wrong_old_program_digest_is_not_downgraded_to_manual_download(self):
        self.seed()
        original = self.database.read_bytes()
        self.old_program.write_bytes(b"wrong isolated bytes")
        with self.assertRaisesRegex(RuntimeError, "stopped before readiness") as raised:
            with recovery.startup_recovery(self.database, current_exe=self.program):
                self.fail("Initialization was entered with a wrong old executable")
        self.assert_uninitialized(original)
        self.assertIn(recovery.PREVIOUS_EXE_NAME, str(raised.exception))
        self.assertIn(recovery.PREVIOUS_EXE_SHA256, str(raised.exception))
        self.assertIn(recovery.MANUAL_RECOVERY_HELP, str(raised.exception))
        failures = list(self.root.glob("update-recovery/*/PREPARATION-FAILED.txt"))
        self.assertEqual(len(failures), 1)
        self.assertIn("SHA-256 mismatch", failures[0].read_text(encoding="utf-8"))

    def test_copy_failure_never_allows_initialization_or_a_prepared_manifest(self):
        self.seed()
        original = self.database.read_bytes()
        with patch.object(recovery, "_copy_exclusive", side_effect=OSError("injected copy failure")):
            with self.assertRaises(RuntimeError):
                with recovery.startup_recovery(self.database, current_exe=self.program, automatic=True):
                    self.fail("Initialization was entered after copy failure")
        self.assert_uninitialized(original)

    def test_snapshot_integrity_failure_never_allows_initialization(self):
        self.seed()
        original = self.database.read_bytes()
        real_connect = sqlite3.connect

        class InvalidSnapshot(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == "PRAGMA integrity_check":
                    return super().execute("SELECT 'injected integrity failure'")
                return super().execute(sql, *args, **kwargs)

        def connect(database, *args, **kwargs):
            if str(database).endswith("database-before-update.sqlite3"):
                kwargs["factory"] = InvalidSnapshot
            return real_connect(database, *args, **kwargs)

        with patch.object(recovery.sqlite3, "connect", side_effect=connect):
            with self.assertRaises(RuntimeError):
                with recovery.startup_recovery(self.database):
                    self.fail("Initialization was entered after snapshot validation failure")
        self.assert_uninitialized(original)

    def test_notice_write_failure_is_fatal_before_database_initialization(self):
        self.seed()
        original = self.database.read_bytes()
        write = recovery._write_exclusive

        def deny_notice(path, text):
            if path.name.startswith("LeyLineBook-update-recovery-"):
                raise PermissionError("injected discoverable notice failure")
            return write(path, text)

        with patch.object(recovery, "_write_exclusive", side_effect=deny_notice):
            with self.assertRaises(RuntimeError):
                with recovery.startup_recovery(self.database):
                    self.fail("Initialization was entered without a discoverable notice")
        self.assert_uninitialized(original)

    def test_manual_replacement_without_old_program_records_download_not_saved(self):
        self.seed()
        self.old_program.unlink()
        with recovery.startup_recovery(self.database, current_exe=self.program):
            pass
        directory, manifest = self.manifest()
        self.assertEqual(manifest["previousExecutableStatus"], "download-required")
        self.assertIsNone(manifest["previousExecutableCopy"])
        self.assertEqual(manifest["previousExecutableDownloadUrl"], recovery.PREVIOUS_EXE_URL)
        self.assertIn("NOT SAVED", (directory / "RECOVERY.txt").read_text(encoding="utf-8-sig"))

    def test_manual_candidate_overwriting_old_filename_is_not_treated_as_old_program(self):
        self.seed()
        self.old_program.write_bytes(b"synthetic candidate replacing the official old filename")
        with recovery.startup_recovery(self.database, current_exe=self.old_program):
            pass
        _, manifest = self.manifest()
        self.assertEqual(manifest["previousExecutableStatus"], "download-required")
        self.assertIsNone(manifest["previousExecutableCopy"])

    def test_automatic_candidate_using_old_filename_still_fails_closed(self):
        self.seed()
        original = self.database.read_bytes()
        self.old_program.write_bytes(b"synthetic candidate replacing the official old filename")
        with self.assertRaises(RuntimeError) as raised:
            with recovery.startup_recovery(self.database, current_exe=self.old_program, automatic=True):
                self.fail("Automatic startup accepted its candidate as the saved old program")
        self.assert_uninitialized(original)
        self.assertIn("requires a separate official", str(raised.exception))
        self.assertIn(recovery.PREVIOUS_EXE_NAME, str(raised.exception))

    def test_diagnostic_write_failure_preserves_original_actionable_error(self):
        self.seed()
        original = self.database.read_bytes()
        self.old_program.unlink()
        with patch.object(recovery, "_write_exclusive", side_effect=PermissionError("diagnostic write unavailable")):
            with self.assertRaises(RuntimeError) as raised:
                with recovery.startup_recovery(self.database, current_exe=self.program, automatic=True):
                    self.fail("Unavailable diagnostics allowed startup")
        self.assert_uninitialized(original)
        self.assertIn(recovery.PREVIOUS_EXE_NAME, str(raised.exception))
        self.assertIn(recovery.MANUAL_RECOVERY_HELP, str(raised.exception))

    def test_crash_rollback_journal_is_preserved_before_any_sqlite_connection(self):
        self.seed()
        script = (
            "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('PRAGMA cache_size=1'); c.execute('BEGIN IMMEDIATE'); "
            "c.execute('UPDATE private_payload SET ciphertext=?', (b'uncommitted'*1024,)); "
            "c.executemany('INSERT INTO private_payload VALUES(?,?)', [(i,b'new'*4096) for i in range(2,200)]); "
            "os._exit(7)"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.database)], cwd=self.root,
            capture_output=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        journal = self.database.with_name(self.database.name + "-journal")
        original, journal_bytes = self.database.read_bytes(), journal.read_bytes()
        self.assertGreater(len(journal_bytes), 512)
        self.assertNotEqual(journal_bytes[:8], b"\0" * 8)
        self.old_program.unlink()
        with patch.object(recovery.sqlite3, "connect") as connect:
            with self.assertRaisesRegex(RuntimeError, "Nonempty rollback journal"):
                with recovery.startup_recovery(self.database, current_exe=self.program, automatic=True):
                    self.fail("Crash journal was allowed to recover before protection")
            connect.assert_not_called()
        self.assertEqual(self.database.read_bytes(), original)
        self.assertEqual(journal.read_bytes(), journal_bytes)
        self.assertFalse((self.root / "update-recovery").exists())

    def test_completed_checkpoint_does_not_depend_on_historical_files_or_exe(self):
        self.seed()
        with recovery.startup_recovery(self.database, current_exe=self.program, automatic=True):
            pass
        directory, _ = self.manifest()
        moved = self.root / "moved-historical-evidence"
        self.assertTrue(directory.is_relative_to(self.root) and moved.is_relative_to(self.root))
        directory.rename(moved)
        self.old_program.unlink()
        marker = self.checkpoint()
        with patch.object(recovery, "_prepare_startup_checkpoint", side_effect=AssertionError("must not repeat checkpoint")):
            with recovery.startup_recovery(self.database, current_exe=self.program, automatic=True):
                pass
        self.assertEqual(self.checkpoint(), marker)

    def test_manually_replaced_unmarked_database_gets_a_new_checkpoint(self):
        self.seed()
        with recovery.startup_recovery(self.database):
            pass
        first, _ = self.manifest()
        replacement = self.root / "replacement.db"
        self.seed(replacement, b"different manually restored database")
        self.assertTrue(replacement.is_relative_to(self.root) and self.database.is_relative_to(self.root))
        os.replace(replacement, self.database)
        with recovery.startup_recovery(self.database):
            pass
        second, manifest = self.manifest()
        self.assertNotEqual(first, second)
        with closing(sqlite3.connect(manifest["snapshotDatabase"])) as snapshot:
            self.assertEqual(snapshot.execute("SELECT ciphertext FROM private_payload").fetchone()[0],
                             b"different manually restored database")

    def test_malformed_checkpoint_is_rejected_instead_of_skipping_or_repeating_protection(self):
        self.seed()
        with recovery.startup_recovery(self.database):
            pass
        valid = self.checkpoint()
        malformed = [
            {**valid, "format": True}, {**valid, "format": 1.0},
            {key: value for key, value in valid.items() if key != "origin"},
            {"format": 1, "checkpoint": "time-contract-v2", "state": "committed"},
            {**valid, "origin": "unknown"}, {**valid, "state": "prepared"},
            {**valid, "recoveryDirectory": "relative-path"},
            {**valid, "recoveryDirectory": None, "manifestSha256": None},
            {**valid, "manifestSha256": "not-a-digest"},
            {**valid, "origin": "fresh"},
        ]
        for marker in malformed:
            with self.subTest(marker=marker):
                with closing(sqlite3.connect(self.database)) as connection:
                    connection.execute("UPDATE app_meta SET value=? WHERE key=?",
                                       (json.dumps(marker), recovery.STARTUP_CHECKPOINT_KEY))
                    connection.commit()
                original = self.database.read_bytes()
                with patch.object(recovery, "_prepare_startup_checkpoint") as prepare:
                    with self.assertRaisesRegex(RuntimeError, "Invalid startup checkpoint"):
                        with recovery.startup_recovery(self.database):
                            self.fail("Malformed checkpoint bypassed recovery")
                    prepare.assert_not_called()
                self.assertEqual(self.database.read_bytes(), original)

    def test_moved_committed_database_can_start_without_accessing_old_evidence(self):
        self.seed()
        with recovery.startup_recovery(self.database):
            pass
        marker = self.checkpoint()
        directory, _ = self.manifest()
        historical = self.root / "renamed-historical-evidence"
        moved_root = self.root / "moved-data"
        moved_root.mkdir()
        moved_db = moved_root / self.database.name
        self.assertTrue(all(path.is_relative_to(self.root) for path in (directory, historical, self.database, moved_db)))
        directory.rename(historical)
        self.database.rename(moved_db)
        with patch.object(recovery, "_sha256", side_effect=AssertionError("committed startup must not hash history")), \
                patch.object(recovery, "_prepare_startup_checkpoint", side_effect=AssertionError("must reuse checkpoint")):
            with recovery.startup_recovery(moved_db) as connection:
                actual = json.loads(connection.execute("SELECT value FROM app_meta WHERE key=?",
                                                       (recovery.STARTUP_CHECKPOINT_KEY,)).fetchone()[0])
                self.assertEqual(actual, marker)

    def test_automatic_without_database_requires_saved_program_before_creation(self):
        self.old_program.unlink()
        with self.assertRaises(RuntimeError):
            with recovery.startup_recovery(self.database, current_exe=self.program, automatic=True):
                self.fail("Automatic fresh startup requires a saved previous program")
        self.assertFalse(self.database.exists())
        self.old_program.write_bytes(self.old_bytes)
        with recovery.startup_recovery(self.database, current_exe=self.program, automatic=True):
            pass
        _, manifest = self.manifest()
        self.assertFalse(manifest["databaseExisted"])
        self.assertIsNone(manifest["snapshotDatabase"])
        self.assertFalse(manifest["snapshotIncludesCredentials"])
        self.assertEqual(manifest["previousExecutableStatus"], "saved")

    def test_missing_database_with_orphan_sidecar_is_not_created(self):
        sidecar = self.database.with_name(self.database.name + "-wal")
        sidecar.write_bytes(b"isolated orphan evidence")
        with self.assertRaises(RuntimeError):
            with recovery.startup_recovery(self.database):
                self.fail("Orphan SQLite evidence must not be replaced")
        self.assertFalse(self.database.exists())
        self.assertEqual(sidecar.read_bytes(), b"isolated orphan evidence")

    def test_legacy_copy_uses_full_snapshot_and_never_overwrites_an_existing_target(self):
        legacy = self.root / "portable.db"
        self.seed(legacy, b"portable encrypted data")
        original = legacy.read_bytes()
        with recovery.startup_recovery(self.database, legacy_db=legacy):
            pass
        self.assertEqual(self.checkpoint()["origin"], "legacy-copy")
        self.assertEqual(legacy.read_bytes(), original)
        with recovery.startup_recovery(self.database, legacy_db=self.root / "absent-unused-legacy.db"):
            pass
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT ciphertext FROM private_payload").fetchone()[0], b"portable encrypted data")

    def test_foreign_key_failure_rolls_back_initialization_and_checkpoint(self):
        self.seed()
        original = self.database.read_bytes()
        with self.assertRaises(RuntimeError):
            with recovery.startup_recovery(self.database) as connection:
                connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
                connection.execute("CREATE TABLE child(parent_id INTEGER REFERENCES parent(id))")
                connection.execute("INSERT INTO child VALUES(123)")
        self.assertEqual(self.database.read_bytes(), original)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='app_meta'").fetchone())

    def test_relative_redirecting_and_hardlinked_database_paths_are_rejected(self):
        for path in (Path("relative.db"), self.root / "programs" / ".." / "redirected.db"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                with recovery.startup_recovery(path):
                    self.fail("Noncanonical database path was accepted")
        self.seed()
        alias = self.root / "hardlinked.db"
        os.link(self.database, alias)
        original = self.database.read_bytes()
        with self.assertRaises(ValueError):
            with recovery.startup_recovery(alias):
                self.fail("A shared physical database path was accepted")
        self.assertEqual(self.database.read_bytes(), original)

    def test_real_initializer_rejects_invalid_health_before_database_or_lock(self):
        self.seed()
        original = self.database.read_bytes()
        self.health().write_bytes(b"existing evidence")
        invalid = ["", "relative.ok", str(self.program_dir / self.health().name), str(self.health())]
        with patch.object(app.tempfile, "gettempdir", return_value=str(self.root)):
            for value in invalid:
                with self.subTest(value=value), patch.object(recovery, "_startup_lock") as lock, \
                        patch.object(app.sqlite3, "connect") as connect, \
                        patch.object(recovery, "_copy_exclusive") as copy:
                    with self.assertRaises((ValueError, FileExistsError)):
                        app.initialize_database(update_health_file=value)
                    lock.assert_not_called()
                    connect.assert_not_called()
                    copy.assert_not_called()
        self.assertEqual(self.database.read_bytes(), original)
        self.assertEqual(self.health().read_bytes(), b"existing evidence")

    def test_real_source_fresh_initializer_accepts_health_without_old_program(self):
        self.old_program.unlink()
        with patch.object(app.sys, "frozen", False, create=True), \
                patch.object(app.tempfile, "gettempdir", return_value=str(self.root)), \
                patch.object(recovery, "_prepare_startup_checkpoint", side_effect=AssertionError("fresh source must not inspect EXEs")):
            app.initialize_database(update_health_file=str(self.health()))
            self.assertFalse(self.health().exists())
            self.assertEqual(self.checkpoint()["origin"], "fresh")
            app._write_update_health_file(str(self.health()))
            self.assertEqual(self.health().read_bytes(), b"ready\n")
            with self.assertRaises(FileExistsError):
                app._write_update_health_file(str(self.health()))
            with self.assertRaises(ValueError):
                app._write_update_health_file("")

    def test_initializer_failure_never_starts_server_window_or_ready(self):
        self.seed()
        original = self.database.read_bytes()

        def fail_after_write(connection, script):
            connection.execute("CREATE TABLE must_rollback(value TEXT)")
            raise RuntimeError("injected initialization failure")

        with patch.object(app.tempfile, "gettempdir", return_value=str(self.root)), \
                patch.object(app, "execute_sql_script", side_effect=fail_after_write), \
                patch.object(app, "create_http_server") as server, \
                patch.object(app, "launch_window") as window, \
                patch.object(app, "_write_update_health_file") as ready:
            with self.assertRaises(RuntimeError):
                app.run_server(0, "window", str(self.health()))
            server.assert_not_called()
            window.assert_not_called()
            ready.assert_not_called()
        self.assertEqual(self.database.read_bytes(), original)
        self.assertFalse(self.health().exists())

    def test_reset_preserves_checkpoint_and_does_not_repeat_recovery(self):
        app.initialize_database()
        marker = self.checkpoint()
        with patch.object(recovery, "_prepare_startup_checkpoint", side_effect=AssertionError("reset must keep its checkpoint")):
            app.reset_database()
        self.assertEqual(self.checkpoint(), marker)

    def test_headless_cli_invalid_health_exits_nonzero_without_initialization(self):
        self.seed()
        original = self.database.read_bytes()
        environment = {**os.environ, "LEYLINEBOOK_DATA_DIR": str(self.root),
                       "TEMP": str(self.root), "TMP": str(self.root)}
        result = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(Path(app.__file__).resolve()),
             "--no-browser", "--port", "0", "--update-health-file", ""],
            cwd=self.root, env=environment, capture_output=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(result.returncode, 1, result.stderr.decode("utf-8", errors="replace"))
        self.assertIn(b"ValueError", result.stderr)
        self.assertEqual(self.database.read_bytes(), original)
        self.assertFalse((self.root / "update-recovery").exists())
        self.assertFalse((self.root / ".task_records.db.startup.lock").exists())

    def test_headless_error_handler_still_exits_one_when_logging_fails(self):
        for stderr_unavailable in (True, False):
            with self.subTest(stderr_unavailable=stderr_unavailable), \
                    patch.object(app.sys, "argv", ["app.py", "--no-browser", "--port", "0"]), \
                    patch.object(app, "is_already_running", return_value=False), \
                    patch.object(app, "run_server", side_effect=RuntimeError("isolated startup failure")), \
                    patch.object(app, "LOG_PATH") as log, \
                    patch.object(app.sys, "stderr", None if stderr_unavailable else unittest.mock.MagicMock()) as stderr, \
                    patch.object(app, "launch_window") as window, \
                    patch.object(app.webbrowser, "open") as browser, \
                    patch.object(app, "_write_update_health_file") as ready:
                log.open.side_effect = OSError("isolated log failure")
                if stderr is not None:
                    stderr.write.side_effect = OSError("isolated stderr failure")
                with self.assertRaises(SystemExit) as raised:
                    app.main()
                self.assertEqual(raised.exception.code, 1)
                window.assert_not_called()
                browser.assert_not_called()
                ready.assert_not_called()

    def test_non_headless_error_handler_preserves_original_exception(self):
        original = RuntimeError("isolated non-headless failure; no window was started")
        with patch.object(app.sys, "argv", ["app.py", "--port", "0"]), \
                patch.object(app, "is_already_running", return_value=False), \
                patch.object(app, "run_server", side_effect=original):
            with self.assertRaises(RuntimeError) as raised:
                app.main()
            self.assertIs(raised.exception, original)

    def test_hardlinked_sqlite_sidecar_is_rejected_before_connecting(self):
        self.seed()
        sentinel = self.root / "unrelated-sentinel"
        sentinel.write_bytes(b"must not be touched by sqlite")
        os.link(sentinel, self.root / "task_records.db-wal")
        with patch.object(recovery.sqlite3, "connect") as connect:
            with self.assertRaises(ValueError):
                with recovery.startup_recovery(self.database):
                    self.fail("A redirected sidecar was accepted")
            connect.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b"must not be touched by sqlite")

    def test_second_process_cannot_enter_pending_startup_but_can_restart_after_commit(self):
        self.seed()
        context = multiprocessing.get_context("spawn")
        ready, release, errors = context.Event(), context.Event(), context.Queue()
        process = context.Process(target=_hold_startup, args=(str(self.database), ready, release, errors))
        process.start()
        try:
            self.assertTrue(ready.wait(10), "Isolated child did not enter its startup transaction")
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                with recovery.startup_recovery(self.database):
                    self.fail("Concurrent startup entered the guarded initialization")
        finally:
            release.set()
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(5)
            errors.close()
            errors.join_thread()
        self.assertEqual(process.exitcode, 0)
        with patch.object(recovery, "_prepare_startup_checkpoint", side_effect=AssertionError("committed child checkpoint must be reused")):
            with recovery.startup_recovery(self.database):
                pass


if __name__ == "__main__":
    unittest.main()
