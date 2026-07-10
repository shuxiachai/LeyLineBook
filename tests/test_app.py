import errno
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import app


def create_test_account(payload):
    return app.create_account({"proxyUntil": "2099-12-31", **payload})


def seed_test_fixture(connection):
    accounts = [
        ("寒秋爱喵酱", "每日+好感"), ("aaaa", "每日+好感"),
        ("雪惊霄", "每日"), ("绷带", "每日"), ("米姐", "每日+好感"),
        ("咸鱼", "全套每日"), ("浅色山茶", "捡材料"), ("树下", "捡材料"),
        ("十日缘香", "狗粮"), ("虚空王座", ""), ("魈fish", ""),
        ("快乐的手残废鱼", ""),
    ]
    account_ids = {}
    for sort_order, (name, daily_task) in enumerate(accounts):
        cursor = connection.execute(
            "INSERT INTO accounts(name, sort_order, created_at) VALUES(?, ?, ?)",
            (name, sort_order, "2026-06-19T00:00:00"),
        )
        account_ids[name] = cursor.lastrowid
        if daily_task:
            connection.execute(
                "INSERT INTO tasks(account_id, name, recurrence, created_at) VALUES(?, ?, 'daily', ?)",
                (cursor.lastrowid, daily_task, "2026-06-19T00:00:00"),
            )
        for task_name in ("深渊", "剧诗", "危战"):
            connection.execute(
                "INSERT INTO tasks(account_id, name, recurrence, created_at) VALUES(?, ?, 'manual', ?)",
                (cursor.lastrowid, task_name, "2026-06-19T00:00:00"),
            )

    for name in ("aaaa", "雪惊霄", "绷带", "米姐", "咸鱼", "浅色山茶", "树下", "十日缘香"):
        daily = connection.execute(
            "SELECT id FROM tasks WHERE account_id = ? AND recurrence = 'daily' LIMIT 1",
            (account_ids[name],),
        ).fetchone()
        task_id = daily[0] if daily else connection.execute(
            "INSERT INTO tasks(account_id, name, recurrence, created_at) VALUES(?, '每日记录', 'daily', ?)",
            (account_ids[name], "2026-06-19T00:00:00"),
        ).lastrowid
        connection.execute(
            "INSERT INTO task_records(task_id, task_date, completed_at) VALUES(?, '2026-06-18', '2026-06-18T23:59:00')",
            (task_id,),
        )


class TaskRecorderTest(unittest.TestCase):
    def setUp(self):
        # Pin game_today to a fixed date within the 2026-05-20 version cycle so
        # configure_fixed_tasks always initialises next_due values consistently,
        # regardless of when the tests are actually run.
        self._game_today_patch = patch("app.game_today", return_value=date(2026, 6, 15))
        self._game_today_patch.start()

        self.temp_dir = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(self.temp_dir.name) / "test.db"
        app.initialize_database()
        with app.db_connection() as connection:
            seed_test_fixture(connection)
            connection.execute(
                "DELETE FROM app_meta WHERE key = 'daily_task_classification_v1'"
            )
            app.configure_fixed_tasks(connection)
            app.configure_daily_task_classification(connection)
            app.configure_task_groups(connection)

    def tearDown(self):
        self._game_today_patch.stop()
        self.temp_dir.cleanup()

    def test_fixture_creates_accounts_tasks_and_history(self):
        with app.db_connection() as connection:
            account_count = connection.execute(
                "SELECT COUNT(*) FROM accounts WHERE active = 1"
            ).fetchone()[0]
            task_count = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE active = 1"
            ).fetchone()[0]
            record_count = connection.execute("SELECT COUNT(*) FROM task_records").fetchone()[0]

        self.assertGreaterEqual(account_count, 12)
        self.assertGreater(task_count, 12)
        self.assertGreater(record_count, 0)

    def test_new_database_stays_empty_after_restart(self):
        app.DB_PATH = Path(self.temp_dir.name) / "empty.db"
        app.initialize_database()
        with app.db_connection() as connection:
            account_count = connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            task_count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            record_count = connection.execute("SELECT COUNT(*) FROM task_records").fetchone()[0]
        self.assertEqual((account_count, task_count, record_count), (0, 0, 0))

    def test_account_credentials_are_encrypted_and_hidden_from_state(self):
        account = create_test_account({"name": "凭据测试号主"})
        with patch.object(app, "dpapi_protect", return_value="encrypted-payload") as protect:
            app.set_account_credentials(
                account["id"],
                {"username": "player@example.com", "password": "secret", "note": "天空岛"},
            )
        protect.assert_called_once()

        with app.db_connection() as connection:
            stored = connection.execute(
                "SELECT credentials FROM accounts WHERE id = ?", (account["id"],)
            ).fetchone()[0]
        self.assertEqual(stored, "encrypted-payload")

        decrypted = '{"username":"player@example.com","password":"secret","note":"天空岛"}'
        with patch.object(app, "dpapi_unprotect", return_value=decrypted):
            credentials = app.get_account_credentials(account["id"])
        self.assertEqual(credentials["password"], "secret")

        state_account = next(
            item for item in app.load_state("2026-06-22")["accounts"] if item["id"] == account["id"]
        )
        self.assertNotIn("credentials", state_account)

    def test_backup_import_replaces_data_and_reset_clears_it(self):
        existing = create_test_account({"name": "导入前号主"})
        payload = {
            "accounts": [
                {
                    "id": 501,
                    "name": "导入后号主",
                    "owner": "测试",
                    "notes": "",
                    "proxy_until": "2026-08-20",
                    "active": 1,
                    "deleted": 0,
                    "sort_order": 1,
                    "created_at": "2026-06-22T00:00:00",
                    "credentials": "must-not-import",
                }
            ],
            "tasks": [],
            "records": [],
            "storyTasks": [],
            "customTags": [],
            "groupNotes": [],
        }

        app.import_backup(payload)
        with app.db_connection() as connection:
            accounts = connection.execute(
                "SELECT id, name, credentials FROM accounts ORDER BY id"
            ).fetchall()
        self.assertEqual([(row["id"], row["name"]) for row in accounts], [(501, "导入后号主")])
        self.assertIsNone(accounts[0]["credentials"])
        self.assertNotEqual(existing["id"], accounts[0]["id"])

        app.reset_database()
        with app.db_connection() as connection:
            counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("accounts", "tasks", "task_records", "story_tasks", "custom_task_tags")
            )
        self.assertEqual(counts, (0, 0, 0, 0, 0))

    def test_existing_database_schema_adds_weekly_without_losing_data(self):
        app.DB_PATH = Path(self.temp_dir.name) / "legacy.db"
        with sqlite3.connect(app.DB_PATH) as connection:
            connection.executescript(
                """
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, owner TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
                    name TEXT NOT NULL,
                    recurrence TEXT NOT NULL CHECK(recurrence IN ('daily', 'interval', 'manual', 'once', 'monthly', 'version')),
                    interval_days INTEGER, monthly_day INTEGER, next_due TEXT,
                    notes TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
                CREATE TABLE task_records (
                    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
                    task_date TEXT NOT NULL, completed_at TEXT NOT NULL,
                    previous_next_due TEXT, note TEXT NOT NULL DEFAULT '',
                    UNIQUE(task_id, task_date)
                );
                INSERT INTO accounts(id, name, created_at) VALUES(1, '旧号主', '2026-06-01T00:00:00');
                INSERT INTO tasks(id, account_id, name, recurrence, created_at)
                VALUES(1, 1, '旧任务', 'daily', '2026-06-01T00:00:00');
                INSERT INTO task_records(id, task_id, task_date, completed_at)
                VALUES(1, 1, '2026-06-01', '2026-06-01T12:00:00');
                """
            )
        connection.close()

        app.initialize_database()
        with app.db_connection() as connection:
            schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
            ).fetchone()[0]
            task_name = connection.execute("SELECT name FROM tasks WHERE id=1").fetchone()[0]
            record_count = connection.execute("SELECT COUNT(*) FROM task_records").fetchone()[0]
        self.assertIn("'weekly'", schema)
        self.assertEqual(task_name, "旧任务")
        self.assertEqual(record_count, 1)

    def test_weekly_task_resets_each_monday_at_four(self):
        self.assertEqual(
            app.weekly_cycle_start(
                date(2026, 6, 22), app.datetime(2026, 6, 22, 3, 59)
            ),
            date(2026, 6, 15),
        )
        self.assertEqual(
            app.weekly_cycle_start(
                date(2026, 6, 22), app.datetime(2026, 6, 22, 4, 0)
            ),
            date(2026, 6, 22),
        )

        account = create_test_account({"name": "周任务测试号"})
        app.set_account_task_tag(account["id"], {"tag": "爱可菲料理", "enabled": True})
        wednesday = "2026-06-17"
        friday = "2026-06-19"
        next_monday = "2026-06-22"
        task = next(
            item for item in app.load_state(wednesday)["dueTasks"]
            if item["account_id"] == account["id"] and item["name"] == "爱可菲料理"
        )
        self.assertFalse(task["completed"])

        app.toggle_task(task["id"], wednesday, True)
        friday_ids = {item["id"] for item in app.load_state(friday)["dueTasks"]}
        self.assertNotIn(task["id"], friday_ids)
        monday_task = next(
            item for item in app.load_state(next_monday)["dueTasks"] if item["id"] == task["id"]
        )
        self.assertFalse(monday_task["completed"])

        app.toggle_task(task["id"], wednesday, False)
        restored = next(
            item for item in app.load_state(friday)["dueTasks"] if item["id"] == task["id"]
        )
        self.assertFalse(restored["completed"])

    def test_daily_task_can_be_completed_and_undone(self):
        account = create_test_account({"name": "测试号", "dailyTask": "每日委托"})
        selected_date = date.today().isoformat()
        state = app.load_state(selected_date)
        task = next(item for item in state["dueTasks"] if item["account_id"] == account["id"])

        app.toggle_task(task["id"], selected_date, True)
        completed_state = app.load_state(selected_date)
        completed_task = next(item for item in completed_state["dueTasks"] if item["id"] == task["id"])
        self.assertTrue(completed_task["completed"])

        app.toggle_task(task["id"], selected_date, False)
        undone_state = app.load_state(selected_date)
        undone_task = next(item for item in undone_state["dueTasks"] if item["id"] == task["id"])
        self.assertFalse(undone_task["completed"])

    def test_daily_summary_only_counts_daily_category_tasks(self):
        selected_date = date.today().isoformat()
        state = app.load_state(selected_date)
        expected = [
            task for task in state["dueTasks"]
            if task["name"] in app.DAILY_CATEGORY_TASKS
        ]
        self.assertEqual(state["summary"]["dailyTotal"], len(expected))
        self.assertEqual(
            state["summary"]["dailyCompleted"],
            sum(1 for task in expected if task["completed"]),
        )
        self.assertTrue(any(task["name"] == "深境螺旋" for task in state["dueTasks"]))
        self.assertGreater(state["summary"]["total"], state["summary"]["dailyTotal"])

        target = next(task for task in expected if not task["completed"])
        app.toggle_task(target["id"], selected_date, True)
        completed_state = app.load_state(selected_date)
        self.assertEqual(
            completed_state["summary"]["dailyCompleted"],
            state["summary"]["dailyCompleted"] + 1,
        )

    def test_interval_task_advances_and_restores_due_date(self):
        account = create_test_account({"name": "周期测试号"})
        due_date = date.today().isoformat()
        used_at = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=1)
        task = app.create_task(
            {
                "accountId": account["id"],
                "name": "质变仪",
                "recurrence": "interval",
                "intervalDays": 7,
                "nextDue": due_date,
            }
        )

        app.toggle_task(task["id"], due_date, True, used_at.isoformat(timespec="minutes"))
        with app.db_connection() as connection:
            advanced = connection.execute(
                "SELECT next_due FROM tasks WHERE id = ?", (task["id"],)
            ).fetchone()[0]
        self.assertEqual(advanced, (used_at + timedelta(hours=168)).isoformat(timespec="minutes"))
        cooling_state = app.load_state(due_date)
        completed_task = next(item for item in cooling_state["dueTasks"] if item["id"] == task["id"])
        self.assertTrue(completed_task["completed"])
        self.assertEqual(completed_task["available_at"], advanced)

        app.toggle_task(task["id"], due_date, False)
        with app.db_connection() as connection:
            restored = connection.execute(
                "SELECT next_due FROM tasks WHERE id = ?", (task["id"],)
            ).fetchone()[0]
        self.assertEqual(restored, due_date)

    def test_transformer_future_time_is_cooling_not_due(self):
        account = create_test_account({"name": "冷却测试号"})
        task = app.create_task(
            {
                "accountId": account["id"],
                "name": "质变仪",
                "recurrence": "interval",
                "intervalDays": 7,
                "nextDue": date.today().isoformat(),
            }
        )
        available_at = (datetime.now().replace(second=0, microsecond=0) + timedelta(hours=168)).isoformat(timespec="minutes")
        with app.db_connection() as connection:
            connection.execute("UPDATE tasks SET next_due = ? WHERE id = ?", (available_at, task["id"]))

        state = app.load_state(date.today().isoformat())
        cooling = next(item for item in state["dueTasks"] if item["id"] == task["id"])
        self.assertEqual(cooling["available_at"], available_at)
        self.assertGreater(cooling["cooldown_remaining_seconds"], 0)
        self.assertFalse(cooling["completed"])

    def test_history_returns_completed_record(self):
        account = create_test_account({"name": "历史测试号", "dailyTask": "每日"})
        selected_date = date.today().isoformat()
        task = next(
            task for task in app.load_state(selected_date)["dueTasks"]
            if task["account_id"] == account["id"]
        )
        app.toggle_task(task["id"], selected_date, True)

        history = app.list_history(selected_date, selected_date)
        self.assertTrue(any(row["account_name"] == "历史测试号" for row in history))

    def test_one_time_task_stays_hidden_after_completion(self):
        account = create_test_account({"name": "临时任务测试号"})
        selected_date = date.today().isoformat()
        task = app.create_task(
            {
                "accountId": account["id"],
                "name": "挖矿320",
                "recurrence": "once",
                "nextDue": selected_date,
            }
        )
        app.toggle_task(task["id"], selected_date, True)

        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        tomorrow_task_ids = {item["id"] for item in app.load_state(tomorrow)["dueTasks"]}
        self.assertNotIn(task["id"], tomorrow_task_ids)

    def test_fixed_monthly_tasks_are_configured(self):
        with app.db_connection() as connection:
            theater = connection.execute(
                "SELECT recurrence, monthly_day FROM tasks WHERE name = '幻想真境剧诗' LIMIT 1"
            ).fetchone()
            abyss = connection.execute(
                "SELECT recurrence, monthly_day FROM tasks WHERE name = '深境螺旋' LIMIT 1"
            ).fetchone()
            old_abyss_count = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE name = '深渊'"
            ).fetchone()[0]

        self.assertEqual((theater["recurrence"], theater["monthly_day"]), ("monthly", 1))
        self.assertEqual((abyss["recurrence"], abyss["monthly_day"]), ("monthly", 16))
        self.assertEqual(old_abyss_count, 0)

    def test_daily_tasks_are_reclassified_with_notes(self):
        with app.db_connection() as connection:
            aaaa = connection.execute(
                """
                SELECT t.name, t.notes FROM tasks t JOIN accounts a ON a.id = t.account_id
                WHERE a.name = 'aaaa' AND t.recurrence = 'daily' AND t.active = 1
                """
            ).fetchone()
            snow = connection.execute(
                """
                SELECT t.name, t.notes FROM tasks t JOIN accounts a ON a.id = t.account_id
                WHERE a.name = '雪惊霄' AND t.recurrence = 'daily' AND t.active = 1
                """
            ).fetchone()
            fish = connection.execute(
                """
                SELECT t.name, t.notes FROM tasks t JOIN accounts a ON a.id = t.account_id
                WHERE a.name = '咸鱼' AND t.recurrence = 'daily' AND t.active = 1
                ORDER BY t.name
                """
            ).fetchall()
            material = connection.execute(
                """
                SELECT t.name FROM tasks t JOIN accounts a ON a.id = t.account_id
                WHERE a.name = '浅色山茶' AND t.name = '捡材料' AND t.active = 1
                """
            ).fetchone()

        self.assertEqual((aaaa["name"], aaaa["notes"]), ("体力", "好感队"))
        self.assertEqual((snow["name"], snow["notes"]), ("体力", "委托"))
        self.assertEqual({row["name"] for row in fish}, {"体力", "狗粮"})
        self.assertEqual(next(row["notes"] for row in fish if row["name"] == "体力"), "委托、好感队")
        self.assertIsNone(material)

    def test_monthly_task_advances_to_next_month(self):
        account = create_test_account({"name": "月度测试号"})
        task = app.create_task(
            {
                "accountId": account["id"],
                "name": "月度任务",
                "recurrence": "monthly",
                "monthlyDay": 16,
            }
        )
        due_date = task["next_due"]
        app.toggle_task(task["id"], due_date, True)
        with app.db_connection() as connection:
            advanced = connection.execute(
                "SELECT next_due FROM tasks WHERE id = ?", (task["id"],)
            ).fetchone()[0]
        self.assertEqual(date.fromisoformat(advanced).day, 16)
        self.assertNotEqual(advanced, due_date)

        completed_state = app.load_state(due_date)
        completed_task = next(
            item for item in completed_state["dueTasks"] if item["id"] == task["id"]
        )
        self.assertTrue(completed_task["completed"])

        next_day = (date.fromisoformat(due_date) + timedelta(days=1)).isoformat()
        next_day_ids = {item["id"] for item in app.load_state(next_day)["dueTasks"]}
        self.assertNotIn(task["id"], next_day_ids)

        app.toggle_task(task["id"], due_date, False)
        restored_task = next(
            item for item in app.load_state(due_date)["dueTasks"] if item["id"] == task["id"]
        )
        self.assertFalse(restored_task["completed"])

    def test_version_schedule_calculates_war_window(self):
        settings = app.update_version_start({"versionStartDate": "2026-06-03"})
        self.assertEqual(settings["warWindow"]["eventStart"], "2026-06-10")
        self.assertEqual(settings["warWindow"]["eventEnd"], "2026-07-14")

        state = app.load_state("2026-06-10")
        war_tasks = [task for task in state["dueTasks"] if task["name"] == "危战"]
        self.assertGreater(len(war_tasks), 0)
        task = war_tasks[0]
        app.toggle_task(task["id"], "2026-06-10", True)
        next_day_ids = {item["id"] for item in app.load_state("2026-06-11")["dueTasks"]}
        self.assertNotIn(task["id"], next_day_ids)

    def test_official_anchor_rolls_war_into_each_version(self):
        settings = app.update_version_start({"versionStartDate": "2026-05-20"})
        self.assertEqual(settings["warWindow"]["eventStart"], "2026-05-27")
        self.assertEqual(settings["warWindow"]["eventEnd"], "2026-06-30")
        self.assertEqual(settings["warWindow"]["nextVersionStart"], "2026-07-01")

        first_cycle = app.load_state("2026-05-27")
        war_task = next(task for task in first_cycle["dueTasks"] if task["name"] == "危战")
        app.toggle_task(war_task["id"], "2026-05-27", True)
        same_cycle_ids = {task["id"] for task in app.load_state("2026-05-28")["dueTasks"]}
        next_cycle_ids = {task["id"] for task in app.load_state("2026-07-08")["dueTasks"]}
        self.assertNotIn(war_task["id"], same_cycle_ids)
        self.assertIn(war_task["id"], next_cycle_ids)

    def test_abyss_group_tasks_expose_small_deadline_metadata(self):
        state = app.load_state("2026-06-19")
        abyss = next(task for task in state["dueTasks"] if task["name"] == "深境螺旋")
        theater = next(task for task in state["dueTasks"] if task["name"] == "幻想真境剧诗")
        war = next(task for task in state["dueTasks"] if task["name"] == "危战")
        self.assertEqual((abyss["event_end"], abyss["event_end_time"]), ("2026-07-16", "03:59"))
        self.assertEqual((theater["event_end"], theater["event_end_time"]), ("2026-07-01", "03:59"))
        self.assertEqual((war["event_end"], war["event_end_time"]), ("2026-06-30", "03:59"))

    def test_account_tasks_and_notes_can_be_toggled_as_tags(self):
        account = create_test_account({"name": "标签测试号"})
        app.set_account_task_tag(account["id"], {"tag": "体力", "enabled": True, "notes": ["好感队"]})
        app.set_account_task_tag(account["id"], {"tag": "深境螺旋", "enabled": True})

        tasks = [
            task for task in app.load_state(date.today().isoformat())["tasks"]
            if task["account_id"] == account["id"]
        ]
        stamina = next(task for task in tasks if task["name"] == "体力")
        abyss = next(task for task in tasks if task["name"] == "深境螺旋")
        self.assertEqual(stamina["notes"], "好感队")
        self.assertEqual((abyss["recurrence"], abyss["monthly_day"]), ("monthly", 16))

        app.set_task_notes(stamina["id"], {"notes": ["好感队", "委托", "好感队"]})
        app.set_task_notes(abyss["id"], {"notes": ["满星"]})
        updated_tasks = [
            task for task in app.load_state(date.today().isoformat())["tasks"]
            if task["account_id"] == account["id"]
        ]
        self.assertEqual(next(task for task in updated_tasks if task["name"] == "体力")["notes"], "好感队、委托")
        self.assertEqual(next(task for task in updated_tasks if task["name"] == "深境螺旋")["notes"], "满星")

        app.set_account_task_tag(account["id"], {"tag": "体力", "enabled": False})
        active_names = {
            task["name"] for task in app.load_state(date.today().isoformat())["tasks"]
            if task["account_id"] == account["id"]
        }
        self.assertNotIn("体力", active_names)

    def test_task_notes_keep_weekly_boss_and_resin_entries(self):
        account = create_test_account({"name": "周本备注测试号"})
        app.set_account_task_tag(account["id"], {"tag": "体力", "enabled": True})
        stamina = next(
            task for task in app.load_state(date.today().isoformat())["tasks"]
            if task["account_id"] == account["id"] and task["name"] == "体力"
        )
        app.set_task_notes(
            stamina["id"],
            {"notes": ["好感队", "周本:吞星之鲸", "树脂:120@2026-06-15T12:00"]},
        )

        updated = next(
            task for task in app.load_state(date.today().isoformat())["tasks"]
            if task["account_id"] == account["id"] and task["name"] == "体力"
        )
        self.assertEqual(updated["notes"], "好感队、周本:吞星之鲸、树脂:120@2026-06-15T12:00")

    def test_new_task_and_notes_are_saved_together(self):
        account = create_test_account({"name": "任务草稿测试号"})
        before = [
            task for task in app.load_state(date.today().isoformat())["tasks"]
            if task["account_id"] == account["id"]
        ]
        self.assertEqual(before, [])

        app.set_account_task_tag(
            account["id"],
            {"tag": "体力", "enabled": True, "notes": ["好感队", "委托"]},
        )
        saved = [
            task for task in app.load_state(date.today().isoformat())["tasks"]
            if task["account_id"] == account["id"]
        ]
        self.assertEqual(len(saved), 1)
        self.assertEqual((saved[0]["name"], saved[0]["notes"]), ("体力", "好感队、委托"))

        app.set_account_task_tag(
            account["id"],
            {"tag": "体力", "enabled": True, "notes": ["委托"]},
        )
        updated = next(
            task for task in app.load_state(date.today().isoformat())["tasks"]
            if task["account_id"] == account["id"]
        )
        self.assertEqual(updated["notes"], "委托")

    def test_account_order_can_be_rearranged_and_persists(self):
        selected_date = date.today().isoformat()
        initial_state = app.load_state(selected_date)
        reversed_ids = [account["id"] for account in reversed(initial_state["accounts"])]

        app.reorder_accounts({"accountIds": reversed_ids})
        reordered_state = app.load_state(selected_date)
        self.assertEqual(
            [account["id"] for account in reordered_state["accounts"]],
            reversed_ids,
        )

        due_account_ids = []
        for task in reordered_state["dueTasks"]:
            if task["account_id"] not in due_account_ids:
                due_account_ids.append(task["account_id"])
        self.assertEqual(
            due_account_ids,
            [account_id for account_id in reversed_ids if account_id in due_account_ids],
        )

        with self.assertRaisesRegex(ValueError, "重复"):
            app.reorder_accounts({"accountIds": [reversed_ids[0], reversed_ids[0]]})

    def test_care_plan_applies_tasks_on_account_creation_then_decouples(self):
        plan = app.create_care_plan({"name": "普托", "tasks": ["体力", "狗粮", "壶"]})
        self.assertEqual(plan["tasks"], ["体力", "狗粮", "壶"])

        account = app.create_account(
            {"name": "方案测试号", "proxyUntil": "2099-12-31", "planId": plan["id"]}
        )
        tasks = [
            task for task in app.load_state(date.today().isoformat())["tasks"]
            if task["account_id"] == account["id"]
        ]
        self.assertEqual({task["name"] for task in tasks}, {"体力", "狗粮", "壶"})
        teapot = next(task for task in tasks if task["name"] == "壶")
        self.assertEqual((teapot["recurrence"], teapot["interval_days"]), ("interval", 3))

        app.update_care_plan(plan["id"], {"name": "精托", "tasks": ["体力"]})
        app.delete_care_plan(plan["id"])
        tasks_after = [
            task for task in app.load_state(date.today().isoformat())["tasks"]
            if task["account_id"] == account["id"]
        ]
        self.assertEqual({task["name"] for task in tasks_after}, {"体力", "狗粮", "壶"})

    def test_care_plan_rejects_invalid_tasks(self):
        with self.assertRaisesRegex(ValueError, "无效任务"):
            app.create_care_plan({"name": "坏方案", "tasks": ["体力", "不存在的任务"]})
        with self.assertRaisesRegex(ValueError, "至少"):
            app.create_care_plan({"name": "空方案", "tasks": []})

    def test_care_plan_activity_switch_enables_current_activities(self):
        big = app.create_custom_tag(
            {"name": "方案活动A", "category": "大活动", "durationDays": 16,
             "startDate": date.today().isoformat()}
        )
        app.create_custom_tag(
            {"name": "方案活动B", "category": "小活动", "durationDays": 7,
             "startDate": date.today().isoformat()}
        )
        plan = app.create_care_plan({"name": "活动托", "tasks": ["体力", "大活动"]})
        account = app.create_account(
            {"name": "活动方案号", "proxyUntil": "2099-12-31", "planId": plan["id"]}
        )
        tasks = [
            task for task in app.load_state(date.today().isoformat())["tasks"]
            if task["account_id"] == account["id"]
        ]
        names = {task["name"] for task in tasks}
        self.assertEqual(names, {"体力", "方案活动A"})
        activity_task = next(task for task in tasks if task["name"] == "方案活动A")
        self.assertEqual(activity_task["custom_tag_id"], big["id"])

    def test_food_task_exposes_previous_day_completion_time(self):
        account = create_test_account({"name": "狗粮时间测试号"})
        app.set_account_task_tag(account["id"], {"tag": "狗粮", "enabled": True})
        task = next(
            item for item in app.load_state("2026-06-20")["dueTasks"]
            if item["account_id"] == account["id"] and item["name"] == "狗粮"
        )
        self.assertNotIn("prev_day_completed_at", task)

        app.toggle_task(task["id"], "2026-06-20", True)
        next_day = next(
            item for item in app.load_state("2026-06-21")["dueTasks"] if item["id"] == task["id"]
        )
        with app.db_connection() as connection:
            recorded = connection.execute(
                "SELECT completed_at FROM task_records WHERE task_id = ? AND task_date = '2026-06-20'",
                (task["id"],),
            ).fetchone()[0]
        self.assertEqual(next_day["prev_day_completed_at"], recorded)

        two_days_later = next(
            item for item in app.load_state("2026-06-22")["dueTasks"] if item["id"] == task["id"]
        )
        self.assertNotIn("prev_day_completed_at", two_days_later)

    def test_teapot_early_collect_restarts_cycle(self):
        account = create_test_account({"name": "壶提前收取测试号"})
        app.set_account_task_tag(account["id"], {"tag": "壶", "enabled": True})
        task = next(
            item for item in app.load_state("2026-06-20")["tasks"]
            if item["account_id"] == account["id"] and item["name"] == "壶"
        )

        app.toggle_task(task["id"], "2026-06-20", True)
        after_normal = next(
            item for item in app.load_state("2026-06-20")["tasks"] if item["id"] == task["id"]
        )
        self.assertEqual(after_normal["next_due"], "2026-06-23")

        app.toggle_task(task["id"], "2026-06-21", True, restart_cycle=True)
        after_early = next(
            item for item in app.load_state("2026-06-21")["tasks"] if item["id"] == task["id"]
        )
        self.assertEqual(after_early["next_due"], "2026-06-24")

    def test_daily_and_abyss_groups_support_tasks(self):
        account = create_test_account({"name": "分组测试号"})
        app.set_account_task_tag(account["id"], {"tag": "质变仪", "enabled": True})
        app.set_account_task_tag(account["id"], {"tag": "壶", "enabled": True})

        state = app.load_state(date.today().isoformat())
        tasks = [task for task in state["tasks"] if task["account_id"] == account["id"]]
        transformer = next(task for task in tasks if task["name"] == "质变仪")
        teapot = next(task for task in tasks if task["name"] == "壶")
        self.assertEqual((transformer["recurrence"], transformer["interval_days"]), ("interval", 7))
        self.assertEqual((teapot["recurrence"], teapot["interval_days"]), ("interval", 3))

    def test_activity_name_cannot_shadow_builtin_task(self):
        with self.assertRaisesRegex(ValueError, "不能与内置任务重名"):
            app.create_custom_tag(
                {
                    "name": "体力",
                    "category": "大活动",
                    "durationDays": 16,
                    "startDate": date.today().isoformat(),
                }
            )

    def test_custom_activity_tasks_are_linked_and_deleted_by_id(self):
        account = create_test_account({"name": "活动关联测试号"})
        app.set_account_task_tag(account["id"], {"tag": "体力", "enabled": True})
        first = app.create_custom_tag(
            {
                "name": "活动甲",
                "category": "大活动",
                "durationDays": 16,
                "startDate": date.today().isoformat(),
            }
        )
        second = app.create_custom_tag(
            {
                "name": "活动乙",
                "category": "小活动",
                "durationDays": 7,
                "startDate": date.today().isoformat(),
            }
        )
        app.set_account_custom_tag(account["id"], {"tagId": first["id"], "enabled": True})
        app.set_account_custom_tag(account["id"], {"tagId": second["id"], "enabled": True})

        with app.db_connection() as connection:
            linked = connection.execute(
                "SELECT name, custom_tag_id FROM tasks WHERE account_id = ? AND custom_tag_id IS NOT NULL ORDER BY name",
                (account["id"],),
            ).fetchall()
        self.assertEqual(
            [(row["name"], row["custom_tag_id"]) for row in linked],
            [("活动乙", second["id"]), ("活动甲", first["id"])],
        )

        app.delete_custom_tag(first["id"])
        with app.db_connection() as connection:
            stamina_active = connection.execute(
                "SELECT active FROM tasks WHERE account_id = ? AND name = '体力'",
                (account["id"],),
            ).fetchone()[0]
            first_active = connection.execute(
                "SELECT active FROM tasks WHERE account_id = ? AND name = '活动甲'",
                (account["id"],),
            ).fetchone()[0]
            second_active = connection.execute(
                "SELECT active FROM tasks WHERE account_id = ? AND custom_tag_id = ?",
                (account["id"], second["id"]),
            ).fetchone()[0]
        self.assertEqual((stamina_active, first_active, second_active), (1, 0, 1))

    def test_existing_activity_tasks_receive_tag_id_during_migration(self):
        account = create_test_account({"name": "旧活动迁移测试号"})
        tag = app.create_custom_tag(
            {
                "name": "旧活动",
                "category": "大活动",
                "durationDays": 16,
                "startDate": date.today().isoformat(),
            }
        )
        with app.db_connection() as connection:
            task_id = connection.execute(
                """
                INSERT INTO tasks(account_id, name, recurrence, next_due, sort_order, created_at)
                VALUES(?, '旧活动', 'once', ?, 0, ?)
                """,
                (account["id"], date.today().isoformat(), app.now_text()),
            ).lastrowid
            app.migrate_activity_task_links(connection)
            linked_id = connection.execute(
                "SELECT custom_tag_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()[0]
        self.assertEqual(linked_id, tag["id"])

    def test_today_task_order_matches_account_configuration(self):
        account = create_test_account({"name": "任务顺序测试号"})
        for name in reversed(tuple(app.TASK_TAG_PRESETS)):
            app.set_account_task_tag(account["id"], {"tag": name, "enabled": True})
        first = app.create_custom_tag(
            {
                "name": "顺序活动甲",
                "category": "大活动",
                "durationDays": 16,
                "startDate": date.today().isoformat(),
            }
        )
        second = app.create_custom_tag(
            {
                "name": "顺序活动乙",
                "category": "小活动",
                "durationDays": 7,
                "startDate": date.today().isoformat(),
            }
        )
        app.set_account_custom_tag(account["id"], {"tagId": second["id"], "enabled": True})
        app.set_account_custom_tag(account["id"], {"tagId": first["id"], "enabled": True})

        tasks = [
            task["name"]
            for task in app.load_state(date.today().isoformat())["tasks"]
            if task["account_id"] == account["id"]
        ]
        self.assertEqual(
            tasks,
            [
                "体力", "狗粮", "质变仪", "壶", "爱可菲料理",
                "探索派遣",
                "顺序活动甲", "顺序活动乙",
                "深境螺旋", "幻想真境剧诗", "危战",
            ],
        )

    def test_expedition_uses_selected_hour_cooldown(self):
        account = create_test_account({"name": "派遣测试号"})
        app.set_account_task_tag(
            account["id"],
            {"tag": "探索派遣", "enabled": True, "notes": ["派遣:15小时"]},
        )
        selected_date = date.today().isoformat()
        task = next(
            item for item in app.load_state(selected_date)["dueTasks"]
            if item["account_id"] == account["id"] and item["name"] == "探索派遣"
        )
        used_at = datetime.now().replace(second=0, microsecond=0)

        app.toggle_task(task["id"], selected_date, True, used_at.isoformat(timespec="minutes"))

        with app.db_connection() as connection:
            next_due = connection.execute(
                "SELECT next_due FROM tasks WHERE id = ?", (task["id"],)
            ).fetchone()[0]
        self.assertEqual(
            next_due,
            (used_at + timedelta(hours=15)).isoformat(timespec="minutes"),
        )
        completed = next(
            item for item in app.load_state(selected_date)["dueTasks"]
            if item["id"] == task["id"]
        )
        self.assertTrue(completed["completed"])

    def test_manual_tasks_are_migrated_to_visible_one_time_tasks(self):
        account = create_test_account({"name": "旧任务迁移测试号"})
        with app.db_connection() as connection:
            task_id = connection.execute(
                """
                INSERT INTO tasks(account_id, name, recurrence, created_at)
                VALUES(?, '旧专项任务', 'manual', ?)
                """,
                (account["id"], app.now_text()),
            ).lastrowid
            app.migrate_manual_tasks_to_once(connection)

        state = app.load_state(date.today().isoformat())
        migrated = next(item for item in state["dueTasks"] if item["id"] == task_id)
        self.assertEqual(migrated["recurrence"], "once")

    def test_story_bonus_deadlines_follow_version_phases(self):
        self.assertEqual(
            app.story_bonus_deadline("archon", datetime(2026, 6, 1, 12, 0)),
            "2026-06-30T15:00",
        )
        self.assertEqual(
            app.story_bonus_deadline("legend", datetime(2026, 6, 1, 12, 0)),
            "2026-06-09T15:00",
        )
        self.assertEqual(
            app.story_bonus_deadline("legend", datetime(2026, 6, 21, 12, 0)),
            "2026-06-30T15:00",
        )
        self.assertIsNone(app.story_bonus_deadline("world", datetime(2026, 6, 1)))

    def test_story_tasks_complete_reopen_and_keep_optional_deadline(self):
        account = create_test_account({"name": "剧情任务测试号"})
        archon = app.create_story_task(
            {
                "accountId": account["id"],
                "name": "新的魔神任务",
                "taskType": "archon",
                "hasBonus": True,
            }
        )
        world = app.create_story_task(
            {
                "accountId": account["id"],
                "name": "普通世界任务",
                "taskType": "world",
                "hasBonus": True,
            }
        )
        self.assertIsNotNone(archon["bonus_deadline"])
        self.assertIsNone(world["bonus_deadline"])
        self.assertEqual(world["has_bonus"], 0)

        guest = app.create_story_task(
            {
                "ownerName": "临时剧情号主",
                "name": "",
                "taskType": "legend",
                "hasBonus": False,
            }
        )
        self.assertIsNone(guest["account_id"])
        self.assertEqual(guest["name"], "传说任务")
        listed_guest = next(task for task in app.list_story_tasks() if task["id"] == guest["id"])
        self.assertEqual(listed_guest["account_name"], "临时剧情号主")

        app.toggle_story_task(archon["id"], True)
        completed = next(task for task in app.list_story_tasks() if task["id"] == archon["id"])
        self.assertIsNotNone(completed["completed_at"])
        app.toggle_story_task(archon["id"], False)
        reopened = next(task for task in app.list_story_tasks() if task["id"] == archon["id"])
        self.assertIsNone(reopened["completed_at"])
        self.assertEqual(reopened["bonus_deadline"], archon["bonus_deadline"])

        app.archive_story_task(world["id"])
        self.assertNotIn(world["id"], {task["id"] for task in app.list_story_tasks()})

    def test_story_schema_migration_allows_temporary_owner(self):
        account = create_test_account({"name": "旧剧情号主"})
        with app.db_connection() as connection:
            connection.executescript(
                """
                DROP TABLE story_tasks;
                CREATE TABLE story_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    name TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    has_bonus INTEGER NOT NULL DEFAULT 0,
                    bonus_deadline TEXT,
                    completed_at TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO story_tasks(account_id, name, task_type, created_at)
                VALUES(?, '旧剧情任务', 'world', ?)
                """,
                (account["id"], app.now_text()),
            )
            app.migrate_story_tasks_schema(connection)
            columns = {row[1]: row for row in connection.execute("PRAGMA table_info(story_tasks)")}
            preserved = connection.execute("SELECT name, account_id, owner_name FROM story_tasks").fetchone()
        self.assertEqual(columns["account_id"][3], 0)
        self.assertIn("owner_name", columns)
        self.assertEqual((preserved["name"], preserved["account_id"], preserved["owner_name"]), ("旧剧情任务", account["id"], ""))


    def test_server_uses_available_port_when_preferred_port_is_denied(self):
        class FakeServer:
            attempts = []

            def __init__(self, address, _handler):
                self.attempts.append(address)
                if address[1] == 8765:
                    raise OSError(errno.EACCES, "port denied")
                self.server_address = (address[0], 49152)

        with patch.object(app, "ExclusiveThreadingHTTPServer", FakeServer):
            server, selected_port = app.create_http_server(8765)

        self.assertIsInstance(server, FakeServer)
        self.assertEqual(selected_port, 49152)
        self.assertEqual(FakeServer.attempts, [("127.0.0.1", 8765), ("127.0.0.1", 0)])

    def test_update_check_prefers_matching_windows_exe_asset(self):
        class FakeResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "tag_name": "v1.5.0",
                        "assets": [
                            {"name": "LeyLineBook-helper.exe", "browser_download_url": "wrong"},
                            {
                                "name": "LeyLineBook-v1.5.0-Windows-x64.exe",
                                "browser_download_url": "right",
                            },
                        ],
                    }
                ).encode()

        with patch.object(app, "APP_VERSION", "1.4.0"), patch.object(
            app.urllib.request, "urlopen", return_value=FakeResponse()
        ):
            result = app.check_for_update()

        self.assertTrue(result["hasUpdate"])
        self.assertEqual(result["latest"], "1.5.0")
        self.assertEqual(result["downloadUrl"], "right")


class SecurityTest(unittest.TestCase):
    def _make_handler(self, headers, path="/", port=8765):
        handler = app.RequestHandler.__new__(app.RequestHandler)
        handler.headers = headers
        handler.path = path
        handler.sent_errors = []
        handler.api_called = False
        handler.static_args = []

        class _Srv:
            server_address = ("127.0.0.1", port)

        handler.server = _Srv()
        handler.send_error = lambda code, *a, **k: handler.sent_errors.append(code)
        handler.handle_api = lambda *a, **k: setattr(handler, "api_called", True)
        handler.serve_static = lambda p: handler.static_args.append(p)
        return handler

    def test_foreign_host_header_is_rejected(self):
        handler = self._make_handler({"Host": "attacker.example.com"}, path="/")
        handler.dispatch("GET")
        self.assertIn(app.HTTPStatus.FORBIDDEN, handler.sent_errors)
        self.assertEqual(handler.static_args, [])

    def test_localhost_host_header_is_accepted(self):
        for host in ("127.0.0.1:8765", "localhost:8765"):
            handler = self._make_handler({"Host": host}, path="/")
            handler.dispatch("GET")
            self.assertEqual(handler.sent_errors, [])
            self.assertEqual(handler.static_args, ["/"])

    def test_cross_origin_write_is_rejected(self):
        handler = self._make_handler(
            {"Host": "127.0.0.1:8765", "Origin": "http://evil.example.com"},
            path="/api/reset",
        )
        handler.dispatch("POST")
        self.assertIn(app.HTTPStatus.FORBIDDEN, handler.sent_errors)
        self.assertFalse(handler.api_called)

    def test_same_origin_write_is_allowed(self):
        handler = self._make_handler(
            {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765"},
            path="/api/care-plans",
        )
        handler.read_json = lambda: {}
        handler.dispatch("POST")
        self.assertEqual(handler.sent_errors, [])
        self.assertTrue(handler.api_called)

    def test_static_path_traversal_is_blocked(self):
        handler = self._make_handler({"Host": "127.0.0.1:8765"})
        handler.serve_static = app.RequestHandler.serve_static.__get__(handler)
        app.RequestHandler.serve_static(handler, "/../app.py")
        self.assertIn(app.HTTPStatus.FORBIDDEN, handler.sent_errors)

    def test_malicious_version_tag_is_rejected(self):
        class FakeResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"tag_name": "v1.0.0 && calc", "assets": []}).encode()

        with patch.object(app.urllib.request, "urlopen", return_value=FakeResponse()):
            with self.assertRaisesRegex(ValueError, "非法版本号"):
                app.check_for_update()

    def test_update_not_offered_for_same_or_older_version(self):
        def make_response(tag):
            class FakeResponse:
                headers = {}

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self):
                    return json.dumps({"tag_name": tag, "assets": []}).encode()

            return FakeResponse()

        with patch.object(app, "APP_VERSION", "3.0.0"):
            with patch.object(app.urllib.request, "urlopen", return_value=make_response("v3.0.0")):
                self.assertFalse(app.check_for_update()["hasUpdate"])
            with patch.object(app.urllib.request, "urlopen", return_value=make_response("v2.9.9")):
                self.assertFalse(app.check_for_update()["hasUpdate"])

    def test_update_download_url_host_is_whitelisted(self):
        app._latest_release = {
            "downloadUrl": "https://evil.example.com/LeyLineBook.exe",
            "latest": "9.9.9",
        }
        with patch.object(app.sys, "frozen", True, create=True):
            with self.assertRaisesRegex(ValueError, "非法下载地址"):
                app.start_update()
        app._latest_release = None


if __name__ == "__main__":
    unittest.main()
