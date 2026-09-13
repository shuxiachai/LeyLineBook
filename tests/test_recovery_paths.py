import ctypes
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import update_recovery as recovery


@unittest.skipUnless(os.name == "nt", "Real Windows path identity tests")
class WindowsRecoveryPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="leylinebook-path-regression-")
        self.addCleanup(self.temp.cleanup)
        self.raw_root = Path(self.temp.name)
        self.root = self.raw_root.resolve()
        self.database = self.root / "long recovery database name.sqlite3"

    def short_path(self, path, *, file_alias=False):
        api = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
        api.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        api.restype = ctypes.c_uint32
        size = api(str(path), None, 0)
        if not size:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(size)
        length = api(str(path), buffer, size)
        if not length or length >= size:
            self.fail(f"GetShortPathNameW failed or changed size: {length}/{size}")
        alias = Path(buffer.value)
        self.assertTrue(alias.samefile(path))
        if alias == path.resolve() or (file_alias and alias.name == path.name):
            self.skipTest(f"Filesystem did not provide a distinct 8.3 alias: {path} -> {alias}")
        return alias

    def seed(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE payload(value TEXT)")
            connection.execute("INSERT INTO payload VALUES('before startup')")
            connection.commit()

    def assert_blocked_before_connect(self, path):
        with patch.object(recovery.sqlite3, "connect") as connect:
            with self.assertRaises((ValueError, RuntimeError)):
                with recovery.startup_recovery(path):
                    self.fail("Unsafe path entered initialization")
            connect.assert_not_called()

    def symlink(self, link, target, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as error:
            if error.winerror == 1314:
                self.skipTest("Windows did not grant symlink creation privilege (1314)")
            raise
        self.addCleanup(lambda: link.unlink(missing_ok=True))
        self.assertTrue(link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)

    def test_existing_short_ancestor_and_uncreated_children_are_canonical(self):
        alias = self.short_path(self.root)
        self.assertEqual(recovery._absolute_path(alias), self.root)
        database = alias / "not-created" / "nested" / "test.db"
        canonical = self.root / "not-created" / "nested" / "test.db"
        self.assertEqual(recovery._absolute_path(database), canonical)
        self.assertFalse(canonical.parent.exists())
        with recovery.startup_recovery(database) as connection:
            self.assertEqual(Path(connection.execute("PRAGMA database_list").fetchone()[2]), canonical)
        self.assertTrue(canonical.exists())
        self.assertTrue((canonical.parent / ".test.db.startup.lock").exists())

    def test_original_temp_spelling_is_accepted_without_changing_temp_environment(self):
        database = self.raw_root / "test.db"
        self.assertEqual(recovery._sqlite_paths(database), self.root / "test.db")
        with recovery.startup_recovery(database):
            pass
        self.assertTrue(database.exists())

    def test_short_database_name_uses_canonical_wal_snapshot_lock_and_manifest(self):
        self.seed()
        alias = self.short_path(self.database, file_alias=True)
        with closing(sqlite3.connect(self.database)) as source:
            source.execute("PRAGMA journal_mode=WAL")
            source.execute("INSERT INTO payload VALUES('committed WAL')")
            source.commit()
            self.assertTrue(Path(str(self.database) + "-wal").exists())
            with recovery.startup_recovery(alias) as connection:
                self.assertEqual(Path(connection.execute("PRAGMA database_list").fetchone()[2]), self.database)
                directory = next((self.root / "update-recovery").iterdir())
                manifest = json.loads((directory / "recovery.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["currentDatabase"], str(self.database))
                self.assertEqual(manifest["sourceDatabase"], str(self.database))
                self.assertEqual(Path(manifest["snapshotDatabase"]), directory / "database-before-update.sqlite3")
                with closing(sqlite3.connect(manifest["snapshotDatabase"])) as snapshot:
                    self.assertEqual(snapshot.execute("SELECT value FROM payload").fetchall(),
                                     [("before startup",), ("committed WAL",)])
                self.assertIn(str(self.database), (directory / "RECOVERY.txt").read_text(encoding="utf-8"))
                connection.execute("INSERT INTO payload VALUES('new write')")
        self.assertTrue((self.root / f".{self.database.name}.startup.lock").exists())
        self.assertFalse((self.root / f".{alias.name}.startup.lock").exists())
        self.assertFalse((self.root / (alias.name + "-wal")).exists())
        with recovery._startup_lock(alias):
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                with recovery._startup_lock(self.database):
                    self.fail("Short and long paths obtained different locks")

    def test_short_database_name_checks_canonical_sidecar_hardlinks(self):
        self.seed()
        alias = self.short_path(self.database, file_alias=True)
        original = self.database.read_bytes()
        sentinel = self.root / "sentinel"
        sentinel.write_bytes(b"isolated sidecar evidence")
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                sidecar = Path(str(self.database) + suffix)
                os.link(sentinel, sidecar)
                try:
                    self.assert_blocked_before_connect(alias)
                    self.assertEqual(sentinel.read_bytes(), b"isolated sidecar evidence")
                    self.assertEqual(self.database.read_bytes(), original)
                finally:
                    sidecar.unlink()

    def test_short_database_name_checks_canonical_nonempty_journal(self):
        self.seed()
        alias = self.short_path(self.database, file_alias=True)
        journal = Path(str(self.database) + "-journal")
        journal.write_bytes(b"preserve possible crash journal")
        original = self.database.read_bytes()
        with patch.object(recovery.sqlite3, "connect") as connect:
            with self.assertRaisesRegex(RuntimeError, "Nonempty rollback journal"):
                with recovery.startup_recovery(alias):
                    self.fail("Short DB spelling hid its long-name journal")
            connect.assert_not_called()
        self.assertEqual(journal.read_bytes(), b"preserve possible crash journal")
        self.assertEqual(self.database.read_bytes(), original)
        self.assertFalse((self.root / "update-recovery").exists())

    def test_short_database_name_preserves_real_crash_journal(self):
        self.seed()
        alias = self.short_path(self.database, file_alias=True)
        script = (
            "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('PRAGMA cache_size=1'); c.execute('BEGIN IMMEDIATE'); "
            "c.execute('UPDATE payload SET value=?', (b'uncommitted'*1024,)); "
            "c.executemany('INSERT INTO payload VALUES(?)', [(b'new'*4096,) for _ in range(200)]); "
            "os._exit(7)"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.database)], cwd=self.root,
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        journal = Path(str(self.database) + "-journal")
        original, journal_bytes = self.database.read_bytes(), journal.read_bytes()
        self.assertGreater(len(journal_bytes), 512)
        self.assertNotEqual(journal_bytes[:8], b"\0" * 8)
        with patch.object(recovery.sqlite3, "connect") as connect:
            with self.assertRaisesRegex(RuntimeError, "Nonempty rollback journal"):
                with recovery.startup_recovery(alias):
                    self.fail("Real crash journal was opened before protection")
            connect.assert_not_called()
        self.assertEqual(self.database.read_bytes(), original)
        self.assertEqual(journal.read_bytes(), journal_bytes)
        self.assertFalse((self.root / "update-recovery").exists())

    def test_short_database_hardlink_and_hardlinked_lock_are_rejected(self):
        self.seed()
        alias = self.short_path(self.database, file_alias=True)
        linked = self.root / "hardlink.sqlite3"
        os.link(self.database, linked)
        try:
            self.assert_blocked_before_connect(alias)
        finally:
            linked.unlink()
        sentinel = self.root / "sentinel"
        sentinel.write_bytes(b"lock must not modify this")
        lock = self.root / f".{self.database.name}.startup.lock"
        os.link(sentinel, lock)
        self.assert_blocked_before_connect(alias)
        self.assertEqual(sentinel.read_bytes(), b"lock must not modify this")

    def test_relative_parent_and_ambiguous_windows_names_still_fail_closed(self):
        alias = self.short_path(self.root)
        for path in (Path("relative.db"), alias / ".." / "other.db",
                     alias / "ambiguous." / "test.db", alias / "ambiguous " / "test.db"):
            with self.subTest(path=path):
                self.assert_blocked_before_connect(path)

    def test_literal_tilde_name_is_not_assumed_to_be_an_alias(self):
        literal = self.root / "not~a-short-alias"
        literal.mkdir()
        database = literal / "test.db"
        self.assertEqual(recovery._absolute_path(database), database)

    def test_symlink_database_and_dangling_leaf_are_rejected(self):
        self.seed()
        link = self.root / "symlink.db"
        self.symlink(link, self.database)
        self.assert_blocked_before_connect(link)
        self.database.unlink()
        self.assertFalse(link.exists())
        self.assert_blocked_before_connect(link)

    def test_symlink_ancestor_is_rejected_before_missing_children(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "directory-link"
        self.symlink(link, target, directory=True)
        self.assert_blocked_before_connect(link / "missing" / "test.db")
        alias = self.short_path(self.root)
        self.assert_blocked_before_connect(alias / link.name / "missing" / "test.db")
        self.assertEqual(list(target.iterdir()), [])

    def test_symlink_sidecars_are_rejected_before_connect(self):
        self.seed()
        sentinel = self.root / "sentinel"
        sentinel.write_bytes(b"must not touch")
        probe = self.root / "symlink-probe"
        self.symlink(probe, sentinel)
        probe.unlink()
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                sidecar = Path(str(self.database) + suffix)
                self.symlink(sidecar, sentinel)
                try:
                    self.assert_blocked_before_connect(self.database)
                finally:
                    sidecar.unlink()
        self.assertEqual(sentinel.read_bytes(), b"must not touch")

    def test_junction_and_dangling_junction_ancestors_are_rejected(self):
        target = self.root / "junction-target"
        target.mkdir()
        link = self.root / "junction"
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "$ErrorActionPreference = 'Stop'; New-Item -ItemType Junction "
             "-Path $env:LLB_TEST_LINK -Target $env:LLB_TEST_TARGET | Out-Null"],
            env={**os.environ, "LLB_TEST_LINK": str(link), "LLB_TEST_TARGET": str(target)},
            capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.addCleanup(link.rmdir)
        self.assertTrue(link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        self.assert_blocked_before_connect(link / "missing" / "test.db")
        alias = self.short_path(self.root)
        self.assert_blocked_before_connect(alias / link.name / "test.db")
        target.rmdir()
        self.assertFalse(link.exists())
        self.assert_blocked_before_connect(link / "missing" / "test.db")

    def test_attribute_access_errors_are_not_treated_as_missing(self):
        with patch.object(Path, "lstat", side_effect=PermissionError("isolated access denial")), \
                patch.object(recovery, "_windows_long_path") as expand:
            with self.assertRaises(PermissionError):
                recovery._absolute_path(self.database)
            expand.assert_not_called()

    def test_resolve_difference_without_win32_long_name_match_is_rejected(self):
        with patch.object(recovery, "_windows_long_path", return_value=self.root / "different"):
            self.assert_blocked_before_connect(self.database)


if __name__ == "__main__":
    unittest.main()
