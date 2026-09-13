import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo
import zoneinfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
IMPORT_DIR = tempfile.TemporaryDirectory(prefix="leyline-time-import-")
with patch.dict(os.environ, {"LEYLINEBOOK_DATA_DIR": IMPORT_DIR.name}):
    app = importlib.import_module("app")


class FutureDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        moment = cls(2027, 1, 1, tzinfo=timezone.utc)
        return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)


class TimeContractTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="leyline-time-test-")
        self.db_patch = patch.object(app, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        self.clock_patch = patch.object(app, "datetime", FutureDateTime)
        self.clock_patch.start()
        app.initialize_database()
        self.account = app.create_account({"name": "Time contract", "proxyUntil": "2099-12-31"})

    def tearDown(self):
        self.clock_patch.stop()
        self.db_patch.stop()
        self.directory.cleanup()

    def task(self, name="质变仪", hours=168):
        return app.create_task({"accountId": self.account["id"], "name": name, "recurrence": "interval", "intervalDays": 7 if hours == 168 else 1, "nextDue": "2026-01-01", "notes": "派遣:15小时" if hours == 15 else ""})

    def test_dst_elapsed_and_undo(self):
        for start in ("2026-03-29T12:00:00+11:00", "2026-09-27T12:00:00+10:00"):
            for hours in (168, 15, 20):
                if hours != 168:
                    start = "2026-04-04T18:00:00+11:00" if "03-29" in start or "04-04" in start else "2026-10-03T18:00:00+10:00"
                with self.subTest(start=start, hours=hours):
                    task = self.task("质变仪" if hours == 168 else "探索派遣", hours)
                    app.toggle_task(task["id"], start[:10], True, start)
                    backup = app.build_backup_payload()
                    stored = next(row for row in backup["data"]["tasks"] if row["id"] == task["id"])
                    due = datetime.fromisoformat(stored["next_due"])
                    self.assertEqual((due - datetime.fromisoformat(start)).total_seconds(), hours * 3600)
                    self.assertTrue(stored["next_due"].endswith("Z"))
                    app.toggle_task(task["id"], start[:10], False)
                    restored = next(row for row in app.build_backup_payload()["data"]["tasks"] if row["id"] == task["id"])
                    self.assertEqual(restored["next_due"], "2026-01-01")

    def test_explicit_offsets_distinguish_repeated_hour_and_order(self):
        first = app.optional_local_datetime("2026-04-05T02:30:00+11:00")
        second = app.optional_local_datetime("2026-04-05T02:30:00+10:00")
        self.assertEqual((second - first).total_seconds(), 3600)
        with self.assertRaisesRegex(ValueError, "偏移"):
            app.optional_local_datetime("2026-04-05T02:30")
        rows = [{"id": 1, "task_date": "2026-04-04", "completed_at": app.utc_text(first)}, {"id": 2, "task_date": "2026-04-04", "completed_at": app.utc_text(second)}]
        self.assertEqual(sorted(rows, key=app.record_time_key, reverse=True)[0]["id"], 2)

    def test_boundary_and_calendar_semantics(self):
        due = datetime.fromisoformat("2026-10-04T02:00:00Z")
        for delta in (-1, 0, 1):
            self.assertEqual(app.moment_difference(due, due + timedelta(seconds=delta)), -delta)
        self.assertEqual(app.game_day_end(date(2026, 10, 3)), datetime(2026, 10, 4, 4))
        self.assertEqual(app.moment_difference(datetime(2026, 10, 4, 12), datetime(2026, 9, 27, 12)), 168 * 3600)

    def test_same_second_record_ids_sort_numerically(self):
        rows = [{"id": number, "task_date": "2026-04-04", "completed_at": "2026-04-04T16:30:00Z"} for number in (9, 10)]
        self.assertEqual([row["id"] for row in sorted(rows, key=app.record_time_key, reverse=True)], [10, 9])

    def test_historical_batch_is_atomic_and_requires_actual_time(self):
        task = self.task()
        before = app.build_backup_payload()["data"]
        with self.assertRaisesRegex(ValueError, "实际使用时间"):
            app.complete_all("2026-09-10", [task["id"]])
        self.assertEqual(app.build_backup_payload()["data"], before)

    def test_precise_order_and_undo_use_instants_not_business_dates(self):
        first = datetime.fromisoformat("2026-09-12T14:00:00Z")
        for hours in (15, 20, 168):
            with self.subTest(hours=hours):
                task = self.task("质变仪" if hours == 168 else "探索派遣", hours)
                app.toggle_task(task["id"], "2026-09-13", True, "2026-09-13T04:00:00+14:00")
                first_due = app.utc_text(first + timedelta(hours=hours))
                before = app.build_backup_payload()["data"]
                with self.assertRaisesRegex(ValueError, "尚未到期"):
                    app.toggle_task(task["id"], "2026-09-12", True, app.utc_text(first + timedelta(hours=hours, minutes=-1)))
                self.assertEqual(app.build_backup_payload()["data"], before)
                second = first + timedelta(hours=hours + 1)
                app.toggle_task(task["id"], "2026-09-12", True, second.astimezone(timezone(timedelta(hours=-10))).isoformat())
                after = app.build_backup_payload()["data"]
                records = sorted((row for row in after["records"] if row["task_id"] == task["id"]), key=lambda row: app.record_time_key(row, precise=True), reverse=True)
                self.assertEqual(records[0]["task_date"], "2026-09-12")
                self.assertEqual(records[0]["completed_at"], app.utc_text(second))
                self.assertEqual(records[0]["previous_next_due"], first_due)
                for used, message in ((first + timedelta(hours=1), "较新的"), (second + timedelta(minutes=1), "尚未到期")):
                    with self.assertRaisesRegex(ValueError, message):
                        app.toggle_task(task["id"], "2026-09-25", True, app.utc_text(used))
                    self.assertEqual(app.build_backup_payload()["data"], after)
                with self.assertRaisesRegex(ValueError, "先撤销"):
                    app.toggle_task(task["id"], "2026-09-13", False)
                self.assertEqual(app.build_backup_payload()["data"], after)
                app.toggle_task(task["id"], "2026-09-12", False)
                restored = next(row for row in app.build_backup_payload()["data"]["tasks"] if row["id"] == task["id"])
                self.assertEqual(restored["next_due"], first_due)
                app.toggle_task(task["id"], "2026-09-13", False)
                restored = next(row for row in app.build_backup_payload()["data"]["tasks"] if row["id"] == task["id"])
                self.assertEqual(restored["next_due"], "2026-01-01")

    def test_historical_used_at_is_parsed_once_and_never_defaults_from_blank(self):
        for name, hours in (("质变仪", 168), ("探索派遣", 15)):
            task = self.task(name, hours)
            for value in (None, "", " ", "\t\n", "invalid", "2026-02-30T12:00:00Z"):
                with self.subTest(name=name, value=value):
                    before = app.build_backup_payload()["data"]
                    with self.assertRaises(ValueError), patch.object(app, "optional_local_datetime", wraps=app.optional_local_datetime) as parser:
                        app.toggle_task(task["id"], "2026-09-10", True, value)
                    self.assertEqual(parser.call_count, 1)
                    self.assertEqual(app.build_backup_payload()["data"], before)
            for value in ("2026-09-10T02:00:00Z", "2026-09-10T12:00:00+10:00"):
                with patch.object(app, "optional_local_datetime", wraps=app.optional_local_datetime) as parser:
                    app.toggle_task(task["id"], "2026-09-10", True, value)
                self.assertEqual(parser.call_count, 1)
                record = next(row for row in app.build_backup_payload()["data"]["records"] if row["task_id"] == task["id"])
                self.assertEqual(record["completed_at"], "2026-09-10T02:00:00Z")
                app.toggle_task(task["id"], "2026-09-10", False)
            with patch.object(app, "game_today", return_value=date(2027, 1, 1)):
                app.toggle_task(task["id"], "2027-01-01", True)
            record = next(row for row in app.build_backup_payload()["data"]["records"] if row["task_id"] == task["id"])
            self.assertEqual(record["completed_at"], "2027-01-01T00:00:00Z")

    def test_calendar_order_still_uses_business_dates(self):
        for recurrence, options in (("daily", {}), ("interval", {"intervalDays": 3}), ("monthly", {"monthlyDay": 16})):
            task = app.create_task({"accountId": self.account["id"], "name": "Custom " + recurrence, "recurrence": recurrence, "nextDue": "2026-09-10", **options})
            app.toggle_task(task["id"], "2026-09-13", True)
            if recurrence == "daily":
                app.toggle_task(task["id"], "2026-09-12", True)
            else:
                before = app.build_backup_payload()["data"]
                with self.assertRaisesRegex(ValueError, "较新的"):
                    app.toggle_task(task["id"], "2026-09-12", True, "2026-09-14T00:00:00Z")
                self.assertEqual(app.build_backup_payload()["data"], before)

    def test_iana_confirmation_rejects_invalid_choices_and_accepts_both_folds(self):
        payload = self.legacy_payload()
        payload["records"] = []
        entry = {"key": "tasks/0/next_due", "original": payload["tasks"][0]["next_due"], "local": "2026-04-05T02:30", "instant": "2026-04-04T16:30:00Z", "offsetMinutes": 600}
        invalid = [
            ("Not/A_Zone", entry),
            ("Australia/Sydney", {**entry, "local": "2026-10-04T02:30", "instant": "2026-10-03T16:30:00Z", "offsetMinutes": 600}),
            ("Australia/Sydney", {**entry, "offsetMinutes": 660}),
            ("Asia/Shanghai", entry),
        ]
        for zone, choice in invalid:
            with self.subTest(zone=zone, choice=choice):
                before = app.build_backup_payload()["data"]
                payload["timeResolution"] = {"confirmed": True, "sourceZone": zone, "entries": [choice]}
                with self.assertRaisesRegex(ValueError, "时区"):
                    app.import_backup(payload)
                self.assertEqual(app.build_backup_payload()["data"], before)
                self.assertEqual(list(Path(self.directory.name).glob("pre-import-*.json")), [])
        original_tzpath = zoneinfo.TZPATH
        try:
            zoneinfo.reset_tzpath(())
            ZoneInfo.clear_cache()
            for instant, offset in (("2026-04-04T15:30:00Z", 660), ("2026-04-04T16:30:00Z", 600)):
                payload["timeResolution"] = {"confirmed": True, "sourceZone": "Australia/Sydney", "entries": [{**entry, "instant": instant, "offsetMinutes": offset}]}
                app.import_backup(payload)
                exported = app.build_backup_payload()["data"]
                self.assertEqual(exported["tasks"][0]["next_due"], instant)
                self.assertEqual(exported["timeLegacyArchive"][0], payload["timeResolution"])
        finally:
            zoneinfo.reset_tzpath(original_tzpath)
            ZoneInfo.clear_cache()

    def legacy_payload(self):
        return {"accounts": [{"id": 1, "name": "Legacy"}], "tasks": [{"id": 1, "account_id": 1, "name": "质变仪", "recurrence": "interval", "interval_days": 7, "next_due": "2026-04-05T02:30"}], "records": [{"id": 1, "task_id": 1, "task_date": "2026-03-29", "completed_at": "2026-03-29T03:30", "previous_next_due": "2026-03-29", "note": ""}]}

    def test_legacy_requires_confirmation_preserves_original_and_roundtrips(self):
        payload = self.legacy_payload()
        original = json.loads(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "来源时区"):
            app.import_backup(payload)
        payload["timeResolution"] = {"confirmed": True, "sourceZone": "Australia/Sydney", "entries": [
            {"key": "tasks/0/next_due", "original": "2026-04-05T02:30", "local": "2026-04-05T02:30", "instant": "2026-04-04T16:30:00Z", "offsetMinutes": 600},
            {"key": "records/0/completed_at", "original": "2026-03-29T03:30", "local": "2026-03-29T03:30", "instant": "2026-03-28T16:30:00Z", "offsetMinutes": 660},
        ]}
        app.import_backup(payload)
        exported = app.build_backup_payload()
        self.assertEqual(exported["schemaVersion"], 4)
        self.assertEqual(exported["timeContractVersion"], 2)
        self.assertEqual(exported["data"]["tasks"][0]["next_due"], "2026-04-04T16:30:00Z")
        self.assertEqual(exported["data"]["timeLegacyArchive"][0]["entries"][0]["original"], original["tasks"][0]["next_due"])
        due = datetime.fromisoformat(exported["data"]["tasks"][0]["next_due"])
        self.assertEqual(due.astimezone(ZoneInfo("Australia/Sydney")).timestamp(), due.astimezone(ZoneInfo("Asia/Shanghai")).timestamp())
        app.import_backup(exported)
        self.assertEqual(app.build_backup_payload()["data"]["timeLegacyArchive"], exported["data"]["timeLegacyArchive"])
        self.assertEqual(payload["tasks"], original["tasks"])

    def test_null_completed_at_rejected_before_replacement(self):
        payload = self.legacy_payload()
        payload["records"][0]["completed_at"] = None
        before = app.build_backup_payload()["data"]
        with self.assertRaisesRegex(ValueError, "完成时间不能为空"):
            app.import_backup(payload)
        self.assertEqual(app.build_backup_payload()["data"], before)

    def test_monthly_presets_use_current_cycle_without_changing_custom_start(self):
        for day, expected in ((date(2026, 9, 13), "2026-08-16"), (date(2026, 9, 16), "2026-09-16"), (date(2026, 9, 17), "2026-09-16"), (date(2026, 1, 1), "2025-12-16")):
            with self.subTest(day=day), patch.object(app, "game_today", return_value=day):
                app.set_account_task_tag(self.account["id"], {"tag": "深境螺旋", "enabled": False})
                app.set_account_task_tag(self.account["id"], {"tag": "深境螺旋", "enabled": True})
                task = next(row for row in app.build_backup_payload()["data"]["tasks"] if row["name"] == "深境螺旋" and row["active"])
                self.assertEqual(task["next_due"], expected)
        with patch.object(app, "game_today", return_value=date(2026, 9, 13)):
            custom = app.create_task({"accountId": self.account["id"], "name": "Custom", "recurrence": "monthly", "monthlyDay": 16})
        self.assertEqual(custom["next_due"], "2026-09-16")


if __name__ == "__main__":
    unittest.main()
