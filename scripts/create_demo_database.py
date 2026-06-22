from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


DEMO_DB = ROOT / "demo" / "task_records.demo.db"


def timestamp(day: date, hour: int = 12) -> str:
    return datetime.combine(day, datetime.min.time()).replace(hour=hour).isoformat(timespec="seconds")


def create_demo_database() -> Path:
    DEMO_DB.parent.mkdir(parents=True, exist_ok=True)
    DEMO_DB.unlink(missing_ok=True)
    app.DB_PATH = DEMO_DB
    app.initialize_database()

    today = date.today()
    created_at = timestamp(today - timedelta(days=30), 9)
    version_window = app.version_window(app.OFFICIAL_VERSION_ANCHOR, today)

    with app.db_connection() as connection:
        accounts = [
            ("示例号主·星河", "演示客户 A", "全日常托管，优先消耗体力", today + timedelta(days=30)),
            ("示例号主·云岚", "演示客户 B", "每日任务与深境内容", today + timedelta(days=14)),
            ("剧情体验号", "演示客户 C", "只委托剧情任务，不做每日", None),
            ("短期委托示例", "演示客户 D", "展示临近到期状态", today + timedelta(days=2)),
        ]
        account_ids: dict[str, int] = {}
        for order, (name, owner, notes, proxy_until) in enumerate(accounts):
            cursor = connection.execute(
                """
                INSERT INTO accounts(name, owner, notes, proxy_until, sort_order, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    owner,
                    notes,
                    proxy_until.isoformat() if proxy_until else None,
                    order,
                    created_at,
                ),
            )
            account_ids[name] = cursor.lastrowid

        task_ids: dict[tuple[str, str], int] = {}

        def add_task(
            account: str,
            name: str,
            recurrence: str,
            *,
            interval_days: int | None = None,
            monthly_day: int | None = None,
            next_due: str | None = None,
            notes: str = "",
            sort_order: int = 0,
            custom_tag_id: int | None = None,
        ) -> int:
            cursor = connection.execute(
                """
                INSERT INTO tasks(
                    account_id, name, recurrence, interval_days, monthly_day,
                    next_due, notes, sort_order, custom_tag_id, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_ids[account],
                    name,
                    recurrence,
                    interval_days,
                    monthly_day,
                    next_due,
                    notes,
                    sort_order,
                    custom_tag_id,
                    created_at,
                ),
            )
            task_ids[(account, name)] = cursor.lastrowid
            return cursor.lastrowid

        for account in ("示例号主·星河", "示例号主·云岚", "短期委托示例"):
            add_task(account, "体力", "daily", notes="好感队、委托", sort_order=0)
            add_task(account, "狗粮", "daily", sort_order=1)

        add_task(
            "示例号主·星河",
            "质变仪",
            "interval",
            interval_days=7,
            next_due=f"{(today + timedelta(days=7)).isoformat()}T18:30",
            sort_order=2,
        )
        add_task(
            "示例号主·星河",
            "壶",
            "interval",
            interval_days=3,
            next_due=today.isoformat(),
            sort_order=3,
        )
        add_task("示例号主·星河", "爱可菲料理", "weekly", sort_order=4)

        for account in ("示例号主·星河", "示例号主·云岚"):
            add_task(
                account,
                "深境螺旋",
                "monthly",
                monthly_day=16,
                next_due=app.monthly_occurrence(today, 16).isoformat(),
                sort_order=10,
            )
            add_task(
                account,
                "幻想真境剧诗",
                "monthly",
                monthly_day=1,
                next_due=app.monthly_occurrence(today, 1).isoformat(),
                sort_order=11,
            )
            add_task(
                account,
                "危战",
                "version",
                next_due=version_window["eventStart"],
                sort_order=12,
            )

        event_specs = [
            ("版本主题活动", "大活动", 16, today - timedelta(days=3)),
            ("限时挑战", "小活动", 8, today - timedelta(days=1)),
        ]
        event_ids: dict[str, int] = {}
        for name, category, duration, start in event_specs:
            cursor = connection.execute(
                """
                INSERT INTO custom_task_tags(name, category, duration_days, start_date, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (name, category, duration, start.isoformat(), created_at),
            )
            event_ids[name] = cursor.lastrowid

        for account in ("示例号主·星河", "示例号主·云岚"):
            for event_name in event_ids:
                add_task(
                    account,
                    event_name,
                    "once",
                    next_due=today.isoformat(),
                    sort_order=5,
                    custom_tag_id=event_ids[event_name],
                )

        connection.execute(
            """
            INSERT INTO story_tasks(
                account_id, owner_name, name, task_type, has_bonus,
                bonus_deadline, created_at
            ) VALUES(?, '', ?, 'archon', 1, ?, ?)
            """,
            (
                account_ids["剧情体验号"],
                "示例魔神任务·第一幕",
                f"{version_window['eventEnd']}T15:00",
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO story_tasks(
                account_id, owner_name, name, task_type, has_bonus,
                bonus_deadline, created_at
            ) VALUES(NULL, '临时剧情号主', '示例传说任务', 'legend', 1, ?, ?)
            """,
            (
                f"{min(today + timedelta(days=20), date.fromisoformat(version_window['eventEnd'])).isoformat()}T15:00",
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO story_tasks(
                account_id, owner_name, name, task_type, has_bonus, created_at
            ) VALUES(?, '', '示例世界任务', 'world', 0, ?)
            """,
            (account_ids["剧情体验号"], created_at),
        )

        completed_today = [
            ("示例号主·星河", "体力"),
            ("示例号主·星河", "狗粮"),
            ("示例号主·云岚", "体力"),
            ("短期委托示例", "体力"),
            ("示例号主·星河", "版本主题活动"),
        ]
        for account, task_name in completed_today:
            note = app.weekly_cycle_key(today) if task_name == "爱可菲料理" else ""
            connection.execute(
                """
                INSERT INTO task_records(task_id, task_date, completed_at, note)
                VALUES(?, ?, ?, ?)
                """,
                (task_ids[(account, task_name)], today.isoformat(), timestamp(today, 11), note),
            )

        transformer_used = today - timedelta(days=1)
        connection.execute(
            """
            INSERT INTO task_records(
                task_id, task_date, completed_at, previous_next_due
            ) VALUES(?, ?, ?, ?)
            """,
            (
                task_ids[("示例号主·星河", "质变仪")],
                transformer_used.isoformat(),
                timestamp(transformer_used, 18),
                transformer_used.isoformat(),
            ),
        )

        weekly_id = task_ids[("示例号主·星河", "爱可菲料理")]
        connection.execute(
            """
            INSERT INTO task_records(task_id, task_date, completed_at, note)
            VALUES(?, ?, ?, ?)
            """,
            (weekly_id, today.isoformat(), timestamp(today, 10), app.weekly_cycle_key(today)),
        )

        history_tasks = [
            task_ids[("示例号主·星河", "体力")],
            task_ids[("示例号主·星河", "狗粮")],
            task_ids[("示例号主·云岚", "体力")],
        ]
        for days_ago in range(1, 15):
            record_day = today - timedelta(days=days_ago)
            for index, task_id in enumerate(history_tasks):
                if (days_ago + index) % 4 == 0:
                    continue
                connection.execute(
                    """
                    INSERT INTO task_records(task_id, task_date, completed_at)
                    VALUES(?, ?, ?)
                    """,
                    (task_id, record_day.isoformat(), timestamp(record_day, 20 + index)),
                )

    with sqlite3.connect(DEMO_DB) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"演示数据库校验失败：{result}")
    return DEMO_DB


if __name__ == "__main__":
    path = create_demo_database()
    print(f"已生成演示数据库：{path}")
