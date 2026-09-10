import copy
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app


class DataRegressionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_patch = patch.object(app, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.day_patch = patch.object(app, "game_today", return_value=date(2026, 6, 15))
        self.day_patch.start()
        self.addCleanup(self.day_patch.stop)
        app.initialize_database()

    def task(self, name="Interval", interval=3, notes=""):
        account = app.create_account({"name": name, "proxyUntil": "2099-12-31"})
        with app.db_connection() as connection:
            return connection.execute(
                "INSERT INTO tasks(account_id,name,recurrence,interval_days,next_due,notes,created_at) VALUES(?,?,'interval',?,'2026-06-01',?,'2026-06-01')",
                (account["id"], name, interval, notes),
            ).lastrowid

    def test_shared_backup_contract_rejects_invalid_fields_without_replacing_data(self):
        fixtures = json.loads((Path(__file__).parent / "fixtures/backup_cases.json").read_text(encoding="utf-8"))
        app.import_backup(fixtures["valid"])
        before = app.build_backup_payload()["data"]
        for case in fixtures["invalid"]:
            with self.subTest(case=case):
                payload = copy.deepcopy(fixtures["valid"])
                row = payload[case["collection"]] if case["collection"] == "settings" else payload[case["collection"]][0]
                row[case["field"]] = case["value"]
                with self.assertRaises(ValueError):
                    app.import_backup(payload)
                self.assertEqual(app.build_backup_payload()["data"], before)

    def test_backup_preserves_settings_and_accepts_v2(self):
        app.update_version_start({"versionStartDate": "2026-06-03"})
        backup = app.build_backup_payload()
        app.reset_database()
        app.import_backup(backup)
        self.assertEqual(app.get_schedule_settings()["versionAnchorDate"], "2026-06-03")
        backup["schemaVersion"] = 2
        del backup["data"]["settings"]
        app.import_backup(backup)
        self.assertEqual(app.get_schedule_settings()["versionAnchorDate"], app.OFFICIAL_VERSION_ANCHOR)

    def test_older_undo_cannot_overwrite_newer_cooldown(self):
        task = self.task()
        app.toggle_task(task, "2026-06-01", True)
        app.toggle_task(task, "2026-06-04", True)
        with self.assertRaisesRegex(ValueError, "较新"):
            app.toggle_task(task, "2026-06-01", False)
        self.assertEqual(app.build_backup_payload()["data"]["tasks"][0]["next_due"], "2026-06-07")
        app.toggle_task(task, "2026-06-04", False)
        app.toggle_task(task, "2026-06-01", False)
        self.assertEqual(app.build_backup_payload()["data"]["tasks"][0]["next_due"], "2026-06-01")

    def test_expedition_has_multiple_occurrences_per_day_and_idempotent_retries(self):
        task = self.task("探索派遣", None, "派遣:15小时")
        app.toggle_task(task, "2026-06-14", True, "2026-06-14T04:10")
        app.toggle_task(task, "2026-06-14", True, "2026-06-14T04:10")
        with self.assertRaisesRegex(ValueError, "尚未到期"):
            app.toggle_task(task, "2026-06-14", True, "2026-06-14T05:00")
        app.toggle_task(task, "2026-06-14", True, "2026-06-14T19:20")
        data = app.build_backup_payload()["data"]
        self.assertEqual(len(data["records"]), 2)
        self.assertEqual(data["tasks"][0]["next_due"], "2026-06-15T10:20")
        app.import_backup(app.build_backup_payload())
        self.assertEqual(len(app.build_backup_payload()["data"]["records"]), 2)

    def test_failed_table_rebuild_restores_original_tables_and_records(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.executescript("""
                CREATE TABLE accounts(id INTEGER PRIMARY KEY);
                INSERT INTO accounts VALUES(1);
                CREATE TABLE tasks(id INTEGER PRIMARY KEY, account_id INTEGER, name TEXT, recurrence TEXT, interval_days INTEGER, next_due TEXT, notes TEXT, active INTEGER, sort_order INTEGER, created_at TEXT);
                INSERT INTO tasks VALUES(1,1,'Legacy','daily',NULL,NULL,'',1,0,'2026-06-01');
                CREATE TABLE task_records(id INTEGER PRIMARY KEY, task_id INTEGER, task_date TEXT, completed_at TEXT, previous_next_due TEXT, note TEXT);
                INSERT INTO task_records VALUES(1,1,'2026-06-01','2026-06-01T12:00:00',NULL,'');
            """)
            connection.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ALTER_TABLE else sqlite3.SQLITE_OK)
            with self.assertRaises(sqlite3.DatabaseError):
                app.migrate_tasks_schema(connection)
            connection.set_authorizer(None)
            self.assertEqual(connection.execute("SELECT name FROM tasks").fetchone()[0], "Legacy")
            self.assertEqual(connection.execute("SELECT count(*) FROM task_records").fetchone()[0], 1)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='tasks_v3'").fetchone())

    def test_backup_read_uses_single_explicit_transaction(self):
        statements = []
        connect = sqlite3.connect
        def traced(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection
        with patch.object(app.sqlite3, "connect", side_effect=traced):
            app.build_backup_payload()
        self.assertEqual(statements.count("BEGIN"), 1)
        self.assertEqual(statements.count("COMMIT"), 1)
        self.assertLess(statements.index("BEGIN"), next(i for i, sql in enumerate(statements) if sql.startswith("SELECT")))


class SecurityRegressionTest(unittest.TestCase):
    def test_real_http_credentials_require_session_even_without_origin(self):
        server, port = app.create_http_server(0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(app, "get_account_credentials", return_value={"password": "synthetic-only"}) as get:
                url = f"http://127.0.0.1:{port}/api/accounts/1/credentials"
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(url)
                self.assertEqual(error.exception.code, 401)
                get.assert_not_called()
                request = urllib.request.Request(url, headers={"X-LeyLineBook-Session": server.session_token})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(json.load(response)["data"]["password"], "synthetic-only")
                self.assertEqual(get.call_count, 1)
                with patch.object(app, "_instance_token", return_value=server.session_token):
                    self.assertTrue(app.is_already_running(port))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_unrelated_service_is_not_treated_as_our_instance(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"success":true,"data":{"application":"LeyLineBook","protocol":1}}'
        with patch.object(app, "_instance_token", return_value="a" * 32), patch.object(app.urllib.request, "urlopen", return_value=response):
            self.assertFalse(app.is_already_running(8765))
            with self.assertRaisesRegex(ValueError, "身份"):
                app.stop_running_server(8765)

    def test_health_marker_waits_for_ui_ready_and_is_written_once(self):
        handler = app.RequestHandler.__new__(app.RequestHandler)
        handler.server = SimpleNamespace(ui_ready=False, ready_lock=threading.Lock(), update_health_file="synthetic-marker")
        handler.send_json = Mock()
        with patch.object(app, "_write_update_health_file") as write:
            write.assert_not_called()
            handler.handle_api("POST", "/api/ready", {})
            handler.handle_api("POST", "/api/ready", {})
            write.assert_called_once_with("synthetic-marker")

    def test_startup_does_not_mark_healthy_before_opening_window(self):
        server, port = app.create_http_server(0)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, "initialize_database"), patch.object(app, "create_http_server", return_value=(server, port)), patch.object(app, "_instance_file", return_value=Path(directory) / "session.txt"), patch.object(app, "dpapi_protect", return_value="synthetic-encrypted"), patch.object(app, "_write_update_health_file") as write:
                def window(url):
                    self.assertIn("/#session=", url)
                    write.assert_not_called()
                    return True
                with patch.object(app, "launch_window", side_effect=window), patch("builtins.print"):
                    app.run_server(port, "window", "synthetic-marker")
                write.assert_not_called()

    def test_concurrent_update_requests_start_one_worker(self):
        real_thread = threading.Thread
        release = {"latest": "9.9.9", "downloadUrl": "https://github.com/shuxiachai/LeyLineBook/releases/download/v9.9.9/LeyLineBook-v9.9.9-Windows-x64.exe", "checksumUrl": "https://github.com/shuxiachai/LeyLineBook/releases/download/v9.9.9/LeyLineBook-v9.9.9-Windows-x64.exe.sha256"}
        with patch.object(app, "_latest_release", release), patch.object(app, "_update_state", {"status": "idle"}), patch.object(app.sys, "frozen", True, create=True), patch.object(app.threading, "Thread") as worker:
            threads = [real_thread(target=app.start_update) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(worker.call_count, 1)
            worker.return_value.start.assert_called_once()
