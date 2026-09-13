import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from update_recovery import prepare_update_recovery


class UpdateRecoveryTests(unittest.TestCase):
    def test_complete_wal_snapshot_keeps_credentials_and_never_restores_new_writes(self):
        with tempfile.TemporaryDirectory(prefix="leylinebook-recovery-test-") as temp:
            root = Path(temp)
            database = root / "task_records.db"
            old_exe = root / "old.exe"
            old_exe.write_bytes(b"isolated synthetic executable")
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE account_credentials(id INTEGER, ciphertext BLOB)")
                connection.execute("INSERT INTO account_credentials VALUES(1, ?)", (b"synthetic-encrypted-secret",))
                connection.commit()
                recovery = prepare_update_recovery(database, old_exe, root / "new.exe")
                connection.execute("INSERT INTO account_credentials VALUES(2, ?)", (b"after-snapshot-write",))
                connection.commit()
                self.assertEqual(connection.execute("SELECT count(*) FROM account_credentials").fetchone(), (2,))
            manifest = json.loads((recovery / "recovery.json").read_text())
            self.assertFalse(manifest["automaticDatabaseRestore"])
            snapshot = Path(manifest["snapshotDatabase"])
            with closing(sqlite3.connect(snapshot)) as connection:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchall(), [("ok",)])
                self.assertEqual(connection.execute("SELECT * FROM account_credentials").fetchall(), [(1, b"synthetic-encrypted-secret")])
            notices = list(root.glob("LeyLineBook-update-recovery-*.txt"))
            self.assertEqual(len(notices), 1)
            instructions = notices[0].read_text(encoding="utf-8-sig")
            self.assertIn(str(database), instructions)
            self.assertIn(str(snapshot), instructions)
            self.assertIn("Do NOT open the current DB with the old program", instructions)
            self.assertIn("LEYLINEBOOK_DATA_DIR", instructions)
            self.assertTrue(old_exe.exists())
            self.assertFalse((root / "new.exe").exists())

    def test_invalid_database_aborts_preparation_without_touching_source(self):
        with tempfile.TemporaryDirectory(prefix="leylinebook-recovery-test-") as temp:
            root = Path(temp)
            database = root / "task_records.db"
            database.write_bytes(b"not a sqlite database")
            old_exe = root / "old.exe"
            old_exe.write_bytes(b"synthetic")
            with self.assertRaisesRegex(RuntimeError, "do not update"):
                prepare_update_recovery(database, old_exe, root / "new.exe")
            self.assertEqual(database.read_bytes(), b"not a sqlite database")
            self.assertEqual(old_exe.read_bytes(), b"synthetic")
            self.assertEqual(list(root.glob("LeyLineBook-update-recovery-*.txt")), [])


if __name__ == "__main__":
    unittest.main()
