"""生成演示数据库：全部为虚构数据，可安全用于截图、录屏与项目演示。

覆盖 v3.x 的全部展示要点：多号主、一键完成、质变仪/壶/探索派遣冷却、
壶提前收取、树脂记录、周本备注、限时活动、托管方案、剧情任务、代打到期提醒、历史记录。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


DEMO_DB = ROOT / "demo" / "task_records.demo.db"


def ts(day: date, hour: int = 12, minute: int = 0) -> str:
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute).isoformat(timespec="seconds")


def dt_text(moment: datetime) -> str:
    """任务 next_due 用的精确到分钟的时间戳。"""
    return moment.isoformat(timespec="minutes")


def create_demo_database() -> Path:
    DEMO_DB.parent.mkdir(parents=True, exist_ok=True)
    DEMO_DB.unlink(missing_ok=True)
    app.DB_PATH = DEMO_DB
    app.initialize_database()

    today = app.game_today()
    now = datetime.now()
    created_at = ts(today - timedelta(days=40), 9)
    window = app.version_window(app.OFFICIAL_VERSION_ANCHOR, today)

    with app.db_connection() as connection:
        # ── 号主：涵盖代打到期的全部状态（正常绿 / 一周内黄 / 三天内红 / 永久无徽章）
        accounts = [
            ("星野", "客户 A", "全托，优先体力", today + timedelta(days=45)),
            ("青芷", "客户 A", "全托", today + timedelta(days=45)),
            ("临渊", "客户 B", "普托，只做每日", today + timedelta(days=6)),
            ("白露", "客户 B", "普托", today + timedelta(days=2)),
            ("云舒", "客户 C", "精托，含深渊", today + timedelta(days=30)),
            ("夜航", "客户 C", "精托", today + timedelta(days=30)),
            ("本命号", "自己", "自用，不设到期", None),
            ("松风", "客户 D", "只做剧情", None),
        ]
        account_ids: dict[str, int] = {}
        for order, (name, owner, notes, proxy_until) in enumerate(accounts):
            cur = connection.execute(
                "INSERT INTO accounts(name, owner, notes, proxy_until, sort_order, created_at) VALUES(?,?,?,?,?,?)",
                (name, owner, notes, proxy_until.isoformat() if proxy_until else None, order, created_at),
            )
            account_ids[name] = cur.lastrowid

        task_ids: dict[tuple[str, str], int] = {}

        def add_task(account, name, recurrence, *, interval_days=None, monthly_day=None,
                     next_due=None, notes="", sort_order=0, custom_tag_id=None) -> int:
            cur = connection.execute(
                """INSERT INTO tasks(account_id, name, recurrence, interval_days, monthly_day,
                   next_due, notes, sort_order, custom_tag_id, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (account_ids[account], name, recurrence, interval_days, monthly_day,
                 next_due, notes, sort_order, custom_tag_id, created_at),
            )
            task_ids[(account, name)] = cur.lastrowid
            return cur.lastrowid

        daily_accounts = ["星野", "青芷", "临渊", "白露", "云舒", "夜航", "本命号"]
        premium = ["星野", "青芷", "云舒", "夜航", "本命号"]  # 精托：含质变仪/壶/派遣/周本

        # ── 体力：不同号主展示不同备注组合（好感队/委托/木偶食材/周本/树脂）
        stamina_notes = {
            "星野": "好感队、委托、周本:「公子」、树脂:100@" + dt_text(now - timedelta(hours=3)),
            "青芷": "委托、周本:若陀龙王",
            "临渊": "委托",
            "白露": "好感队",
            "云舒": "好感队、委托、木偶食材、周本:「女士」、树脂:38@" + dt_text(now - timedelta(minutes=40)),
            "夜航": "委托、周本:吞星之鲸",
            "本命号": "好感队、委托、周本:蚀灭的源焰之主",
        }
        for name in daily_accounts:
            add_task(name, "体力", "daily", notes=stamina_notes[name], sort_order=0)
            add_task(name, "狗粮", "daily", sort_order=1)

        # ── 质变仪：星野冷却中（还剩 5 天多）、云舒今天可用
        add_task("星野", "质变仪", "interval", interval_days=7,
                 next_due=dt_text(now + timedelta(days=5, hours=6)), sort_order=2)
        add_task("云舒", "质变仪", "interval", interval_days=7,
                 next_due=today.isoformat(), sort_order=2)

        # ── 壶：星野冷却中（可演示「提前收取」）、青芷今天可收
        add_task("星野", "壶", "interval", interval_days=3,
                 next_due=(today + timedelta(days=2)).isoformat(), sort_order=3)
        add_task("青芷", "壶", "interval", interval_days=3, next_due=today.isoformat(), sort_order=3)

        # 本命号：自用号，一整套都做
        add_task("本命号", "质变仪", "interval", interval_days=7,
                 next_due=dt_text(now + timedelta(days=2, hours=3)), sort_order=2)
        add_task("本命号", "壶", "interval", interval_days=3, next_due=today.isoformat(), sort_order=3)
        add_task("本命号", "探索派遣", "interval", notes="派遣:20小时",
                 next_due=today.isoformat(), sort_order=5)

        # ── 爱可菲料理（每周）
        for name in ("星野", "云舒", "本命号"):
            add_task(name, "爱可菲料理", "weekly", sort_order=4)

        # ── 探索派遣：星野 20 小时冷却中、云舒 15 小时今天可收
        add_task("星野", "探索派遣", "interval", notes="派遣:20小时",
                 next_due=dt_text(now + timedelta(hours=7, minutes=20)), sort_order=5)
        add_task("云舒", "探索派遣", "interval", notes="派遣:15小时",
                 next_due=today.isoformat(), sort_order=5)

        # ── 深渊组
        for name in premium:
            add_task(name, "深境螺旋", "monthly", monthly_day=16,
                     next_due=app.monthly_occurrence(today, 16).isoformat(), sort_order=10)
            add_task(name, "幻想真境剧诗", "monthly", monthly_day=1,
                     next_due=app.monthly_occurrence(today, 1).isoformat(), sort_order=11)
            add_task(name, "危战", "version", next_due=window["eventStart"], sort_order=12)

        # ── 限时活动
        events = [
            ("流明石追踪", "大活动", 16, today - timedelta(days=4)),
            ("镀金旅团的奇幻酒宴", "小活动", 8, today - timedelta(days=1)),
        ]
        event_ids: dict[str, int] = {}
        for name, category, duration, start in events:
            cur = connection.execute(
                "INSERT INTO custom_task_tags(name, category, duration_days, start_date, created_at) VALUES(?,?,?,?,?)",
                (name, category, duration, start.isoformat(), created_at),
            )
            event_ids[name] = cur.lastrowid
        for name in ("星野", "青芷", "云舒", "夜航"):
            for event_name, tag_id in event_ids.items():
                add_task(name, event_name, "once", next_due=today.isoformat(),
                         sort_order=6, custom_tag_id=tag_id)

        # ── 托管方案（v2.3.0 亮点：建号一键套用）
        plans = [
            ("普托", ["体力", "狗粮"]),
            ("精托", ["体力", "狗粮", "质变仪", "壶", "爱可菲料理", "探索派遣", "深境螺旋", "幻想真境剧诗", "危战"]),
            ("全托+活动", ["体力", "狗粮", "质变仪", "壶", "探索派遣", "大活动", "小活动"]),
        ]
        for name, tasks in plans:
            connection.execute(
                "INSERT INTO care_plans(name, tasks, created_at) VALUES(?,?,?)",
                (name, json.dumps(tasks, ensure_ascii=False), created_at),
            )

        # ── 剧情任务
        connection.execute(
            """INSERT INTO story_tasks(account_id, owner_name, name, task_type, has_bonus, bonus_deadline, created_at)
               VALUES(?,'','「炉心探洞」第一幕','archon',1,?,?)""",
            (account_ids["松风"], f"{window['eventEnd']}T15:00", created_at),
        )
        half_end = min(today + timedelta(days=18), date.fromisoformat(window["eventEnd"]))
        connection.execute(
            """INSERT INTO story_tasks(account_id, owner_name, name, task_type, has_bonus, bonus_deadline, created_at)
               VALUES(NULL,'临时委托号','某角色传说任务·第二幕','legend',1,?,?)""",
            (f"{half_end.isoformat()}T15:00", created_at),
        )
        connection.execute(
            """INSERT INTO story_tasks(account_id, owner_name, name, task_type, has_bonus, completed_at, created_at)
               VALUES(?,'','远古的呼唤·世界任务','world',0,?,?)""",
            (account_ids["松风"], ts(today - timedelta(days=2), 21), created_at),
        )

        def record(account, task_name, day, hour=11, minute=0, note=""):
            key = (account, task_name)
            if key not in task_ids:
                return
            connection.execute(
                "INSERT OR IGNORE INTO task_records(task_id, task_date, completed_at, note) VALUES(?,?,?,?)",
                (task_ids[key], day.isoformat(), ts(day, hour, minute), note),
            )

        # ── 今天：部分完成，让进度条呈现「做了一半」的真实感
        for name in ("星野", "青芷"):
            record(name, "体力", today, 10)
            record(name, "狗粮", today, 10, 20)
        record("临渊", "体力", today, 9)
        record("星野", "流明石追踪", today, 10, 40)
        record("星野", "爱可菲料理", today, 10, 50, note=app.weekly_cycle_key(today))

        # ── 昨天的狗粮：用于展示「昨天狗粮采集时间」
        yesterday = today - timedelta(days=1)
        for name in daily_accounts:
            record(name, "狗粮", yesterday, 21, 35)

        # ── 三周历史，让「历史记录」页看起来有内容
        for days_ago in range(1, 22):
            day = today - timedelta(days=days_ago)
            for index, name in enumerate(daily_accounts):
                if (days_ago + index) % 5 == 0:
                    continue  # 偶尔漏做，更真实
                record(name, "体力", day, 20, (index * 7) % 60)
                if (days_ago + index) % 3 != 0:
                    record(name, "狗粮", day, 21, (index * 11) % 60)

    with sqlite3.connect(DEMO_DB) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"演示数据库校验失败：{result}")
        counts = {
            t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("accounts", "tasks", "task_records", "custom_task_tags", "care_plans", "story_tasks")
        }
    print("演示数据统计：", counts)
    return DEMO_DB


if __name__ == "__main__":
    path = create_demo_database()
    print(f"已生成演示数据库：{path}")
