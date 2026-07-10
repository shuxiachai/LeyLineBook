from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RUNTIME_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
STATIC_DIR = RUNTIME_DIR / "static"

_appdata = os.environ.get("APPDATA") if getattr(sys, "frozen", False) else None
DATA_DIR = (Path(_appdata) / "LeyLineBook") if _appdata else APP_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "task_records.db"
LOG_PATH = DATA_DIR / "task_recorder.log"

_legacy_db = APP_DIR / "task_records.db"
if APP_DIR != DATA_DIR and _legacy_db.exists() and not DB_PATH.exists():
    shutil.copy2(_legacy_db, DB_PATH)

TASK_TAG_PRESETS = {
    "体力": {"recurrence": "daily"},
    "狗粮": {"recurrence": "daily"},
    "探索派遣": {"recurrence": "interval"},
    "质变仪": {"recurrence": "interval", "interval_days": 7},
    "壶": {"recurrence": "interval", "interval_days": 3},
    "爱可菲料理": {"recurrence": "weekly"},
    "深境螺旋": {"recurrence": "monthly", "monthly_day": 16},
    "幻想真境剧诗": {"recurrence": "monthly", "monthly_day": 1},
    "危战": {"recurrence": "version"},
}
RESERVED_ACTIVITY_NAMES = frozenset(
    (*TASK_TAG_PRESETS.keys(), "剧诗", "深渊", "捡材料", "尘歌壶")
)
_TASK_SORT_ORDER = {
    "体力": 0, "狗粮": 1, "质变仪": 2, "壶": 3, "爱可菲料理": 4, "探索派遣": 5,
    "深境螺旋": 10, "幻想真境剧诗": 11, "危战": 12,
}
_ACTIVITY_TASK_SORT_ORDER = 6
DAILY_CATEGORY_TASKS = frozenset(("体力", "狗粮", "质变仪", "壶", "爱可菲料理", "探索派遣"))
OFFICIAL_VERSION_ANCHOR = "2026-05-20"
VERSION_LENGTH_DAYS = 42
HEARTBEAT_TIMEOUT = 75
APP_VERSION = "3.0.0"
GITHUB_REPO = "shuxiachai/LeyLineBook"

_last_heartbeat: float = 0.0
_latest_release: dict | None = None
_update_state: dict = {"status": "idle", "downloaded": 0, "total": 0, "error": ""}


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def game_today() -> date:
    return (datetime.now() - timedelta(hours=4)).date()


def today_text() -> str:
    return game_today().isoformat()


def _parse_version(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.lstrip("v").split("."))
    except ValueError:
        return (0,)


def _release_asset_download_url(assets: list[dict], latest_tag: str) -> str | None:
    preferred_name = f"LeyLineBook-v{latest_tag}-Windows-x64.exe"
    for asset in assets:
        if asset.get("name") == preferred_name:
            return asset.get("browser_download_url")
    return next(
        (
            asset.get("browser_download_url")
            for asset in assets
            if str(asset.get("name", "")).lower().endswith(".exe")
        ),
        None,
    )


def check_for_update() -> dict:
    global _latest_release
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": f"LeyLineBook/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode())
    latest_tag = data.get("tag_name", "").lstrip("v")
    if latest_tag and not re.fullmatch(r"[\d.]+", latest_tag):
        raise ValueError(f"非法版本号格式: {latest_tag!r}")
    assets = data.get("assets", [])
    download_url = _release_asset_download_url(assets, latest_tag)
    has_update = bool(latest_tag) and _parse_version(latest_tag) > _parse_version(APP_VERSION)
    result = {
        "current": APP_VERSION,
        "latest": latest_tag,
        "hasUpdate": has_update,
        "downloadUrl": download_url if has_update else None,
    }
    _latest_release = result
    return result


def start_update() -> None:
    global _update_state
    if _update_state["status"] == "downloading":
        return
    if not _latest_release or not _latest_release.get("downloadUrl"):
        raise ValueError("请先检查更新")
    if not getattr(sys, "frozen", False):
        raise ValueError("只有打包后的 EXE 才支持自动更新，请前往 GitHub 手动下载")
    download_url: str = _latest_release["downloadUrl"]
    _allowed_download_hosts = ("https://github.com/", "https://objects.githubusercontent.com/")
    if not any(download_url.startswith(p) for p in _allowed_download_hosts):
        raise ValueError(f"非法下载地址: {download_url!r}")
    current_exe = Path(sys.executable).resolve()
    _update_state = {"status": "downloading", "downloaded": 0, "total": 0, "error": ""}

    def _run() -> None:
        global _update_state
        try:
            temp_exe = Path(tempfile.gettempdir()) / "LeyLineBook-update.exe"
            req = urllib.request.Request(download_url, headers={"User-Agent": f"LeyLineBook/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                _update_state["total"] = total
                downloaded = 0
                with open(temp_exe, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        _update_state["downloaded"] = downloaded
            latest_tag = _latest_release["latest"]
            new_exe = current_exe.parent / f"LeyLineBook-v{latest_tag}-Windows-x64.exe"
            bat_path = Path(tempfile.gettempdir()) / "leylinebook_update.bat"
            delete_old = (
                f'if /i not "{current_exe}" == "{new_exe}" del "{current_exe}"\r\n'
                if current_exe != new_exe else ""
            )
            bat_path.write_text(
                "@echo off\r\n"
                "timeout /t 2 /nobreak > nul\r\n"
                f'move /y "{temp_exe}" "{new_exe}"\r\n'
                f'start "" "{new_exe}"\r\n'
                + delete_old +
                'del "%~f0"\r\n',
                encoding="mbcs",
            )
            _update_state["status"] = "done"
            time.sleep(0.4)
            subprocess.Popen(
                ["cmd.exe", "/c", str(bat_path)],
                creationflags=0x00000008,
                close_fds=True,
            )
            time.sleep(0.6)
            os._exit(0)
        except Exception as exc:
            _update_state["status"] = "error"
            _update_state["error"] = str(exc)

    threading.Thread(target=_run, daemon=True).start()


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_protect(plaintext: str) -> str:
    data = plaintext.encode("utf-8")
    buf = ctypes.create_string_buffer(data, len(data))
    in_blob = _DataBlob(len(data), buf)
    out_blob = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise OSError("DPAPI 加密失败")
    result = bytes(ctypes.string_at(out_blob.pbData, out_blob.cbData))
    ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return base64.b64encode(result).decode("ascii")


def dpapi_unprotect(ciphertext: str) -> str:
    data = base64.b64decode(ciphertext)
    buf = ctypes.create_string_buffer(data, len(data))
    in_blob = _DataBlob(len(data), buf)
    out_blob = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise OSError("凭据解密失败，可能数据来自其他用户或机器")
    result = ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
    ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return result


@contextmanager
def db_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialize_database() -> None:
    with db_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                owner TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                proxy_until TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                deleted INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                name TEXT NOT NULL,
                recurrence TEXT NOT NULL CHECK(recurrence IN ('daily', 'weekly', 'interval', 'manual', 'once', 'monthly', 'version')),
                interval_days INTEGER,
                monthly_day INTEGER,
                next_due TEXT,
                notes TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                custom_tag_id INTEGER REFERENCES custom_task_tags(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                task_date TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                previous_next_due TEXT,
                note TEXT NOT NULL DEFAULT '',
                UNIQUE(task_id, task_date, note)
            );

            CREATE TABLE IF NOT EXISTS account_group_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                category TEXT NOT NULL CHECK(category IN ('daily', 'abyss')),
                note TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(account_id, category, note)
            );

            CREATE TABLE IF NOT EXISTS custom_task_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                category TEXT NOT NULL DEFAULT '大活动',
                duration_days INTEGER NOT NULL DEFAULT 16,
                start_date TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS care_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                tasks TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS story_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER REFERENCES accounts(id),
                owner_name TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL,
                task_type TEXT NOT NULL CHECK(task_type IN ('archon', 'legend', 'world')),
                has_bonus INTEGER NOT NULL DEFAULT 0,
                bonus_deadline TEXT,
                completed_at TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_account ON tasks(account_id, active);
            CREATE INDEX IF NOT EXISTS idx_records_date ON task_records(task_date);
            CREATE INDEX IF NOT EXISTS idx_story_tasks_account ON story_tasks(account_id, active, completed_at);
            """
        )

        migrate_tasks_schema(connection)
        migrate_accounts_schema(connection)
        migrate_custom_tags_schema(connection)
        migrate_story_tasks_schema(connection)
        migrate_activity_task_links(connection)
        migrate_task_preset_sort_order(connection)

        configure_fixed_tasks(connection)
        migrate_manual_tasks_to_once(connection)
        configure_daily_task_classification(connection)
        configure_task_groups(connection)
        migrate_group_notes_to_tasks(connection)


def migrate_tasks_schema(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
    ).fetchone()[0]
    has_monthly_day = "monthly_day" in columns
    if has_monthly_day and "'weekly'" in table_sql:
        return

    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DROP TABLE IF EXISTS tasks_v3")
    connection.execute("DROP TABLE IF EXISTS task_records_v3")
    connection.executescript(
        """
        CREATE TABLE tasks_v3 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            name TEXT NOT NULL,
            recurrence TEXT NOT NULL CHECK(recurrence IN ('daily', 'weekly', 'interval', 'manual', 'once', 'monthly', 'version')),
            interval_days INTEGER,
            monthly_day INTEGER,
            next_due TEXT,
            notes TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE task_records_v3 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks_v3(id),
            task_date TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            previous_next_due TEXT,
            note TEXT NOT NULL DEFAULT '',
            UNIQUE(task_id, task_date, note)
        );
        """
    )
    monthly_day_expression = "monthly_day" if has_monthly_day else "NULL"
    connection.execute(
        f"""
        INSERT INTO tasks_v3(
            id, account_id, name, recurrence, interval_days, monthly_day,
            next_due, notes, active, sort_order, created_at
        )
        SELECT id, account_id, name, recurrence, interval_days, {monthly_day_expression},
               next_due, notes, active, sort_order, created_at
        FROM tasks
        """
    )
    connection.executescript(
        """
        INSERT INTO task_records_v3
        SELECT id, task_id, task_date, completed_at, previous_next_due, note
        FROM task_records;

        DROP TABLE task_records;
        DROP TABLE tasks;
        ALTER TABLE tasks_v3 RENAME TO tasks;
        ALTER TABLE task_records_v3 RENAME TO task_records;
        CREATE INDEX idx_tasks_account ON tasks(account_id, active);
        CREATE INDEX idx_records_date ON task_records(task_date);
        """
    )
    connection.execute("PRAGMA foreign_keys = ON")


def monthly_occurrence(reference: date, day: int) -> date:
    return date(reference.year, reference.month, day)


def next_month_occurrence(reference: date, day: int) -> date:
    if reference.month == 12:
        return date(reference.year + 1, 1, day)
    return date(reference.year, reference.month + 1, day)


def weekly_cycle_start(reference: date, current: datetime | None = None) -> date:
    monday = reference - timedelta(days=reference.weekday())
    moment = current or datetime.now()
    if reference == moment.date() and reference.weekday() == 0 and moment.hour < 4:
        monday -= timedelta(days=7)
    return monday


def weekly_cycle_key(reference: date, current: datetime | None = None) -> str:
    return f"weekly:{weekly_cycle_start(reference, current).isoformat()}"


def version_window(version_anchor: str | None, reference: date | None = None) -> dict | None:
    if not version_anchor:
        return None
    anchor_date = date.fromisoformat(version_anchor)
    reference_date = reference or game_today()
    cycle_offset = (reference_date - anchor_date).days // VERSION_LENGTH_DAYS
    start_date = anchor_date + timedelta(days=cycle_offset * VERSION_LENGTH_DAYS)
    event_start = start_date + timedelta(days=7)
    event_end = start_date + timedelta(days=41)
    next_version_start = start_date + timedelta(days=VERSION_LENGTH_DAYS)
    return {
        "anchorDate": anchor_date.isoformat(),
        "versionStart": start_date.isoformat(),
        "eventStart": event_start.isoformat(),
        "eventStartTime": "10:00",
        "eventEnd": event_end.isoformat(),
        "eventEndTime": "03:59",
        "nextVersionStart": next_version_start.isoformat(),
    }


def get_version_anchor(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT value FROM app_meta WHERE key = 'version_anchor_date'"
    ).fetchone()
    if row:
        return row[0]
    legacy = connection.execute(
        "SELECT value FROM app_meta WHERE key = 'version_start_date'"
    ).fetchone()
    anchor = legacy[0] if legacy else OFFICIAL_VERSION_ANCHOR
    connection.execute(
        "INSERT OR REPLACE INTO app_meta(key, value) VALUES('version_anchor_date', ?)",
        (anchor,),
    )
    return anchor


def configure_fixed_tasks(connection: sqlite3.Connection) -> None:
    reference = game_today()
    theater_due = monthly_occurrence(reference, 1).isoformat()
    abyss_due = monthly_occurrence(reference, 16).isoformat()
    version_anchor = get_version_anchor(connection)
    window = version_window(version_anchor, reference)
    war_due = window["eventStart"] if window else None

    connection.execute(
        """
        UPDATE tasks
        SET name = '幻想真境剧诗', recurrence = 'monthly', monthly_day = 1,
            interval_days = NULL, next_due = ?
        WHERE name IN ('剧诗', '幻想真境剧诗') AND recurrence != 'monthly'
        """,
        (theater_due,),
    )
    connection.execute(
        """
        UPDATE tasks
        SET name = '深境螺旋', recurrence = 'monthly', monthly_day = 16,
            interval_days = NULL, next_due = ?
        WHERE name IN ('深渊', '深境螺旋') AND recurrence != 'monthly'
        """,
        (abyss_due,),
    )
    connection.execute(
        """
        UPDATE tasks
        SET recurrence = 'version', monthly_day = NULL,
            interval_days = NULL, next_due = ?
        WHERE name = '危战' AND recurrence != 'version'
        """,
        (war_due,),
    )


def migrate_manual_tasks_to_once(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE tasks
        SET recurrence = 'once', next_due = COALESCE(next_due, ?)
        WHERE recurrence = 'manual'
        """,
        (game_today().isoformat(),),
    )


def configure_daily_task_classification(connection: sqlite3.Connection) -> None:
    migrated = connection.execute(
        "SELECT value FROM app_meta WHERE key = 'daily_task_classification_v1'"
    ).fetchone()
    if migrated:
        return

    connection.execute(
        """
        UPDATE tasks
        SET name = '体力', notes = '好感队'
        WHERE recurrence = 'daily' AND name = '每日+好感'
        """
    )
    connection.execute(
        """
        UPDATE tasks
        SET name = '体力', notes = '委托'
        WHERE recurrence = 'daily' AND name = '每日'
        """
    )
    full_daily_tasks = connection.execute(
        """
        SELECT id, account_id FROM tasks
        WHERE recurrence = 'daily' AND name = '全套每日' AND active = 1
        """
    ).fetchall()
    for task in full_daily_tasks:
        connection.execute(
            "UPDATE tasks SET name = '体力', notes = '委托、好感队' WHERE id = ?",
            (task["id"],),
        )
        existing_food = connection.execute(
            """
            SELECT id FROM tasks
            WHERE account_id = ? AND recurrence = 'daily' AND name = '狗粮' AND active = 1
            """,
            (task["account_id"],),
        ).fetchone()
        if not existing_food:
            connection.execute(
                """
                INSERT INTO tasks(account_id, name, recurrence, created_at)
                VALUES(?, '狗粮', 'daily', ?)
                """,
                (task["account_id"], now_text()),
            )

    connection.execute(
        """
        UPDATE tasks
        SET name = '体力', notes = ''
        WHERE recurrence = 'daily' AND name = '每日记录'
        """
    )
    connection.execute(
        "INSERT INTO app_meta(key, value) VALUES('daily_task_classification_v1', ?)",
        (now_text(),),
    )


def configure_task_groups(connection: sqlite3.Connection) -> None:
    connection.execute(
        "UPDATE tasks SET active = 0 WHERE name = '捡材料' AND active = 1"
    )
    connection.execute(
        "UPDATE tasks SET name = '壶' WHERE name = '尘歌壶'"
    )
    stamina_tasks = connection.execute(
        "SELECT account_id, notes FROM tasks WHERE name = '体力' AND active = 1"
    ).fetchall()
    for task in stamina_tasks:
        for note in re.split(r"[、,，]", task["notes"] or ""):
            clean_note = note.strip()
            if clean_note:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO account_group_notes(
                        account_id, category, note, created_at
                    ) VALUES(?, 'daily', ?, ?)
                    """,
                    (task["account_id"], clean_note, now_text()),
                )


def migrate_accounts_schema(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(accounts)")}
    if "proxy_until" not in columns:
        connection.execute("ALTER TABLE accounts ADD COLUMN proxy_until TEXT")
    if "deleted" not in columns:
        connection.execute("ALTER TABLE accounts ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
    if "credentials" not in columns:
        connection.execute("ALTER TABLE accounts ADD COLUMN credentials TEXT")


def migrate_custom_tags_schema(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(custom_task_tags)")}
    if "category" not in columns:
        connection.execute("ALTER TABLE custom_task_tags ADD COLUMN category TEXT NOT NULL DEFAULT '大活动'")
    if "duration_days" not in columns:
        connection.execute("ALTER TABLE custom_task_tags ADD COLUMN duration_days INTEGER NOT NULL DEFAULT 16")
    if "start_date" not in columns:
        connection.execute("ALTER TABLE custom_task_tags ADD COLUMN start_date TEXT")


def migrate_story_tasks_schema(connection: sqlite3.Connection) -> None:
    columns = {row[1]: row for row in connection.execute("PRAGMA table_info(story_tasks)")}
    if "owner_name" in columns and columns["account_id"][3] == 0:
        return

    owner_expression = "owner_name" if "owner_name" in columns else "''"
    connection.executescript(
        f"""
        DROP TABLE IF EXISTS story_tasks_v2;
        CREATE TABLE story_tasks_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER REFERENCES accounts(id),
            owner_name TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            task_type TEXT NOT NULL CHECK(task_type IN ('archon', 'legend', 'world')),
            has_bonus INTEGER NOT NULL DEFAULT 0,
            bonus_deadline TEXT,
            completed_at TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        INSERT INTO story_tasks_v2(
            id, account_id, owner_name, name, task_type, has_bonus,
            bonus_deadline, completed_at, active, created_at
        )
        SELECT id, account_id, {owner_expression}, name, task_type, has_bonus,
               bonus_deadline, completed_at, active, created_at
        FROM story_tasks;
        DROP TABLE story_tasks;
        ALTER TABLE story_tasks_v2 RENAME TO story_tasks;
        CREATE INDEX idx_story_tasks_account
        ON story_tasks(account_id, active, completed_at);
        """
    )


def migrate_activity_task_links(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
    if "custom_tag_id" not in columns:
        connection.execute(
            "ALTER TABLE tasks ADD COLUMN custom_tag_id INTEGER REFERENCES custom_task_tags(id) ON DELETE SET NULL"
        )

    reserved = tuple(RESERVED_ACTIVITY_NAMES)
    placeholders = ", ".join("?" for _ in reserved)
    connection.execute(
        f"""
        UPDATE tasks
        SET custom_tag_id = (
            SELECT tag.id FROM custom_task_tags tag WHERE tag.name = tasks.name
        )
        WHERE custom_tag_id IS NULL
          AND recurrence = 'once'
          AND name NOT IN ({placeholders})
          AND EXISTS(SELECT 1 FROM custom_task_tags tag WHERE tag.name = tasks.name)
        """,
        reserved,
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_custom_tag ON tasks(custom_tag_id, account_id, active)"
    )
    connection.execute(
        "UPDATE tasks SET sort_order = ? WHERE custom_tag_id IS NOT NULL AND sort_order != ?",
        (_ACTIVITY_TASK_SORT_ORDER, _ACTIVITY_TASK_SORT_ORDER),
    )


def migrate_task_preset_sort_order(connection: sqlite3.Connection) -> None:
    for name, order in _TASK_SORT_ORDER.items():
        connection.execute(
            "UPDATE tasks SET sort_order = ? WHERE name = ?",
            (order, name),
        )


def migrate_group_notes_to_tasks(connection: sqlite3.Connection) -> None:
    migrated = connection.execute(
        "SELECT value FROM app_meta WHERE key = 'task_specific_notes_v1'"
    ).fetchone()
    if migrated:
        return

    category_tasks = {
        "daily": ("体力",),
        "abyss": ("深境螺旋", "幻想真境剧诗", "危战"),
    }
    for category, task_names in category_tasks.items():
        rows = connection.execute(
            """
            SELECT account_id, note FROM account_group_notes
            WHERE category = ? ORDER BY id
            """,
            (category,),
        ).fetchall()
        notes_by_account: dict[int, list[str]] = {}
        for row in rows:
            notes_by_account.setdefault(row["account_id"], []).append(row["note"])

        placeholders = ", ".join("?" for _ in task_names)
        for account_id, group_notes in notes_by_account.items():
            tasks = connection.execute(
                f"""
                SELECT id, notes FROM tasks
                WHERE account_id = ? AND active = 1 AND name IN ({placeholders})
                """,
                (account_id, *task_names),
            ).fetchall()
            for task in tasks:
                current = [
                    item.strip()
                    for item in re.split(r"[、,，]", task["notes"] or "")
                    if item.strip()
                ]
                merged = list(dict.fromkeys([*current, *group_notes]))
                connection.execute(
                    "UPDATE tasks SET notes = ? WHERE id = ?",
                    ("、".join(merged), task["id"]),
                )

    connection.execute(
        "INSERT INTO app_meta(key, value) VALUES('task_specific_notes_v1', ?)",
        (now_text(),),
    )


def row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def require_text(payload: dict, key: str, max_length: int = 100) -> str:
    value = str(payload.get(key, "")).strip()
    if not value:
        raise ValueError(f"{key} 不能为空")
    if len(value) > max_length:
        raise ValueError(f"{key} 不能超过 {max_length} 个字符")
    return value


def optional_date(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return date.fromisoformat(text).isoformat()


def optional_local_datetime(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        raise ValueError("使用时间应为本地时间")
    parsed = parsed.replace(second=0, microsecond=0)
    if parsed > datetime.now() + timedelta(minutes=5):
        raise ValueError("使用时间不能晚于当前时间")
    return parsed


def exact_due_moment(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if "T" not in text:
        return None
    return datetime.fromisoformat(text)


def get_schedule_settings() -> dict:
    with db_connection() as connection:
        version_anchor = get_version_anchor(connection)
    window = version_window(version_anchor)
    return {
        "versionAnchorDate": version_anchor,
        "versionStartDate": window["versionStart"],
        "warWindow": window,
    }


def update_version_start(payload: dict) -> dict:
    version_start = optional_date(payload.get("versionStartDate"))
    if not version_start:
        raise ValueError("请填写版本更新日期")
    parsed = date.fromisoformat(version_start)
    if parsed.weekday() != 2:
        raise ValueError("版本更新日期应为星期三")
    window = version_window(version_start, parsed)
    with db_connection() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO app_meta(key, value) VALUES('version_anchor_date', ?)",
            (version_start,),
        )
        connection.execute(
            "UPDATE tasks SET next_due = ? WHERE recurrence = 'version' AND active = 1",
            (window["eventStart"],),
        )
    return get_schedule_settings()


def cleanup_expired_activities() -> None:
    with db_connection() as connection:
        expired = connection.execute(
            """
            SELECT id, name FROM custom_task_tags
            WHERE start_date IS NOT NULL
            AND datetime(date(start_date, '+' || duration_days || ' days') || ' 03:59:00')
                < datetime('now', 'localtime')
            """
        ).fetchall()
        for tag in expired:
            connection.execute(
                "UPDATE tasks SET active = 0 WHERE custom_tag_id = ? AND active = 1",
                (tag["id"],),
            )
            connection.execute("DELETE FROM custom_task_tags WHERE id = ?", (tag["id"],))


def _expedition_hours_from_notes(notes: str) -> int:
    for part in str(notes or "").split("、"):
        if part == "派遣:15小时":
            return 15
    return 20


def load_state(selected_date: str) -> dict:
    cleanup_expired_activities()
    date.fromisoformat(selected_date)
    with db_connection() as connection:
        accounts = []
        for row in connection.execute(
            """
            SELECT a.*,
                   COUNT(t.id) AS task_count
            FROM accounts a
            LEFT JOIN tasks t ON t.account_id = a.id AND t.active = 1
            WHERE a.deleted = 0
            GROUP BY a.id
            ORDER BY a.active DESC, a.sort_order, a.id
            """
        ):
            account = row_to_dict(row)
            account.pop("credentials", None)
            accounts.append(account)
        tasks = [
            row_to_dict(row)
            for row in connection.execute(
                """
                SELECT t.*, a.name AS account_name, a.proxy_until AS account_proxy_until,
                       CASE WHEN r.id IS NULL THEN 0 ELSE 1 END AS completed,
                       CASE WHEN EXISTS(
                           SELECT 1 FROM task_records prior WHERE prior.task_id = t.id
                       ) THEN 1 ELSE 0 END AS completed_ever
                FROM tasks t
                JOIN accounts a ON a.id = t.account_id
                LEFT JOIN task_records r
                  ON r.task_id = t.id AND r.task_date = ?
                WHERE t.active = 1 AND a.active = 1 AND a.deleted = 0
                ORDER BY a.sort_order, a.id, t.sort_order,
                         CASE WHEN t.custom_tag_id IS NULL THEN 0 ELSE t.custom_tag_id END,
                         t.id
                """,
                (selected_date,),
            )
        ]
        account_notes = [
            row_to_dict(row)
            for row in connection.execute(
                """
                SELECT account_id, category, note
                FROM account_group_notes
                ORDER BY account_id, category, id
                """
            )
        ]
        custom_tags = [
            row_to_dict(row)
            for row in connection.execute(
                "SELECT * FROM custom_task_tags ORDER BY id"
            )
        ]

    settings = get_schedule_settings()
    war_window = version_window(
        settings["versionAnchorDate"], date.fromisoformat(selected_date)
    )
    selected_day = date.fromisoformat(selected_date)
    selected_cutoff = (
        datetime.now()
        if selected_day == game_today()
        else datetime.combine(selected_day, datetime.max.time())
    )
    selected_week_key = weekly_cycle_key(selected_day)
    with db_connection() as connection:
        completed_version_task_ids = {
            row[0]
            for row in connection.execute(
                """
                SELECT DISTINCT task_id FROM task_records
                WHERE task_date BETWEEN ? AND ?
                """,
                (war_window["eventStart"], war_window["eventEnd"]),
            )
        }
        completed_weekly_task_dates = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT task_id, task_date FROM task_records WHERE note = ?",
                (selected_week_key,),
            )
        }
        prev_day_food_times = {
            row[0]: row[1]
            for row in connection.execute(
                """
                SELECT r.task_id, r.completed_at FROM task_records r
                JOIN tasks t ON t.id = r.task_id
                WHERE t.name = '狗粮' AND r.task_date = ? AND r.note = ''
                """,
                ((selected_day - timedelta(days=1)).isoformat(),),
            )
        }
        completed_monthly_task_ids = {
            row[0]
            for row in connection.execute(
                """
                SELECT DISTINCT r.task_id FROM task_records r
                JOIN tasks t ON t.id = r.task_id
                WHERE t.recurrence = 'monthly' AND t.active = 1
                  AND r.task_date >= t.next_due
                """
            )
        }
    due_tasks = []
    all_tasks = []
    for task in tasks:
        task["completed"] = bool(task["completed"])
        task["completed_ever"] = bool(task["completed_ever"])
        recurrence = task["recurrence"]
        all_tasks.append(task)
        if recurrence == "daily":
            if task["name"] == "狗粮" and task["id"] in prev_day_food_times:
                task["prev_day_completed_at"] = prev_day_food_times[task["id"]]
            due_tasks.append(task)
        elif recurrence == "weekly":
            week_done = task["id"] in completed_weekly_task_dates
            completed_on_selected = completed_weekly_task_dates.get(task["id"]) == selected_date
            task["completed"] = completed_on_selected
            next_refresh = weekly_cycle_start(selected_day) + timedelta(days=7)
            task["event_end"] = next_refresh.isoformat()
            task["event_end_time"] = "04:00"
            if not week_done or completed_on_selected:
                due_tasks.append(task)
        elif recurrence == "interval":
            precise_due = exact_due_moment(task["next_due"]) if task["name"] in ("质变仪", "探索派遣") else None
            if (
                task["name"] == "壶"
                and task["next_due"]
                and "T" not in task["next_due"]
                and not task["completed"]
            ):
                _pot_day = date.fromisoformat(task["next_due"])
                _pot_due = datetime(_pot_day.year, _pot_day.month, _pot_day.day, 4, 0)
                if _pot_due > datetime.now():
                    precise_due = _pot_due
            is_due = bool(
                task["next_due"]
                and (
                    precise_due <= selected_cutoff
                    if precise_due
                    else task["next_due"] <= selected_date
                )
            )
            if precise_due:
                task["available_at"] = precise_due.isoformat(timespec="minutes")
                task["cooldown_remaining_seconds"] = max(
                    0, int((precise_due - datetime.now()).total_seconds())
                )
            still_cooling = precise_due is not None and precise_due > datetime.now()
            if task["completed"] or is_due or still_cooling:
                due_tasks.append(task)
        elif recurrence == "monthly" and (
            task["completed"] or (task["next_due"] and task["next_due"] <= selected_date)
        ) and (task["id"] not in completed_monthly_task_ids or task["completed"]):
            selected = date.fromisoformat(selected_date)
            monthly_day = task["monthly_day"]
            if selected.day < monthly_day:
                deadline = monthly_occurrence(selected, monthly_day)
            else:
                deadline = next_month_occurrence(selected, monthly_day)
            task["event_end"] = deadline.isoformat()
            task["event_end_time"] = "03:59"
            due_tasks.append(task)
        elif recurrence == "version" and (
            war_window["eventStart"] <= selected_date <= war_window["eventEnd"]
        ) and (task["id"] not in completed_version_task_ids or task["completed"]):
            task["next_due"] = war_window["eventStart"]
            task["event_end"] = war_window["eventEnd"]
            task["event_end_time"] = war_window["eventEndTime"]
            due_tasks.append(task)
        elif recurrence == "once" and task["next_due"] and task["next_due"] <= selected_date and (
            not task["completed_ever"] or task["completed"]
        ):
            due_tasks.append(task)

    _next_day = game_today() + timedelta(days=1)
    end_of_game_today = datetime(_next_day.year, _next_day.month, _next_day.day, 4, 0)
    def _long_cooling(task):
        return not task["completed"] and task.get("available_at") and datetime.fromisoformat(task["available_at"]) >= end_of_game_today
    countable_tasks = [task for task in due_tasks if not _long_cooling(task)]
    completed_count = sum(1 for task in countable_tasks if task["completed"])
    daily_tasks = [task for task in countable_tasks if task["name"] in DAILY_CATEGORY_TASKS]
    daily_completed_count = sum(1 for task in daily_tasks if task["completed"])
    return {
        "date": selected_date,
        "accounts": accounts,
        "tasks": all_tasks,
        "dueTasks": due_tasks,
        "accountNotes": account_notes,
        "customTags": custom_tags,
        "carePlans": list_care_plans(),
        "storyTasks": list_story_tasks(),
        "settings": settings,
        "summary": {
            "total": len(countable_tasks),
            "completed": completed_count,
            "remaining": len(countable_tasks) - completed_count,
            "dailyTotal": len(daily_tasks),
            "dailyCompleted": daily_completed_count,
        },
    }


def list_history(start_date: str, end_date: str, account_id: int | None = None) -> list[dict]:
    date.fromisoformat(start_date)
    date.fromisoformat(end_date)
    account_filter = "AND a.id = ?" if account_id else ""
    params = (start_date, end_date, account_id) if account_id else (start_date, end_date)
    with db_connection() as connection:
        return [
            row_to_dict(row)
            for row in connection.execute(
                f"""
                SELECT r.id, r.task_date, r.completed_at, r.note,
                       t.name AS task_name, a.name AS account_name
                FROM task_records r
                JOIN tasks t ON t.id = r.task_id
                JOIN accounts a ON a.id = t.account_id
                WHERE r.task_date BETWEEN ? AND ? {account_filter}
                ORDER BY r.task_date DESC, r.completed_at DESC, r.id DESC
                """,
                params,
            )
        ]


def _parse_proxy_until(payload: dict) -> str | None:
    raw = str(payload.get("proxyUntil", "")).strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        raise ValueError("截止日期格式无效")


CARE_PLAN_ACTIVITY_CATEGORIES = ("大活动", "小活动")


def _parse_care_plan_tasks(payload: dict) -> str:
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("方案至少需要勾选一个任务")
    tasks = []
    for value in raw_tasks:
        task_name = str(value).strip()
        if task_name not in TASK_TAG_PRESETS and task_name not in CARE_PLAN_ACTIVITY_CATEGORIES:
            raise ValueError(f"方案中包含无效任务：{task_name}")
        if task_name not in tasks:
            tasks.append(task_name)
    return json.dumps(tasks, ensure_ascii=False)


def list_care_plans() -> list[dict]:
    with db_connection() as connection:
        plans = []
        for row in connection.execute("SELECT * FROM care_plans ORDER BY id"):
            plan = row_to_dict(row)
            plan["tasks"] = json.loads(plan["tasks"])
            plans.append(plan)
        return plans


def create_care_plan(payload: dict) -> dict:
    name = require_text(payload, "name", 20)
    tasks_json = _parse_care_plan_tasks(payload)
    with db_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO care_plans(name, tasks, created_at) VALUES(?, ?, ?)",
            (name, tasks_json, now_text()),
        )
        plan = row_to_dict(
            connection.execute("SELECT * FROM care_plans WHERE id = ?", (cursor.lastrowid,)).fetchone()
        )
        plan["tasks"] = json.loads(plan["tasks"])
        return plan


def update_care_plan(plan_id: int, payload: dict) -> None:
    name = require_text(payload, "name", 20)
    tasks_json = _parse_care_plan_tasks(payload)
    with db_connection() as connection:
        cursor = connection.execute(
            "UPDATE care_plans SET name = ?, tasks = ? WHERE id = ?",
            (name, tasks_json, plan_id),
        )
        if cursor.rowcount == 0:
            raise LookupError("没有找到该托管方案")


def delete_care_plan(plan_id: int) -> None:
    with db_connection() as connection:
        cursor = connection.execute("DELETE FROM care_plans WHERE id = ?", (plan_id,))
        if cursor.rowcount == 0:
            raise LookupError("没有找到该托管方案")


def create_account(payload: dict) -> dict:
    name = require_text(payload, "name")
    owner = str(payload.get("owner", "")).strip()[:100]
    notes = str(payload.get("notes", "")).strip()[:500]
    proxy_until = _parse_proxy_until(payload)
    with db_connection() as connection:
        max_order = connection.execute("SELECT COALESCE(MAX(sort_order), 0) FROM accounts").fetchone()[0]
        cursor = connection.execute(
            "INSERT INTO accounts(name, owner, notes, proxy_until, sort_order, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (name, owner, notes, proxy_until, max_order + 1, now_text()),
        )
        account_id = cursor.lastrowid
        if payload.get("dailyTask"):
            connection.execute(
                """
                INSERT INTO tasks(account_id, name, recurrence, created_at)
                VALUES(?, ?, 'daily', ?)
                """,
                (account_id, str(payload["dailyTask"])[:100], now_text()),
            )
        if payload.get("planId"):
            try:
                plan_id = int(payload["planId"])
            except (TypeError, ValueError) as error:
                raise ValueError("托管方案选择无效") from error
            plan = connection.execute(
                "SELECT tasks FROM care_plans WHERE id = ?", (plan_id,)
            ).fetchone()
            if not plan:
                raise ValueError("托管方案不存在，请刷新后重试")
            for task_name in json.loads(plan["tasks"]):
                if task_name in TASK_TAG_PRESETS:
                    _insert_preset_task(connection, account_id, task_name)
                elif task_name in CARE_PLAN_ACTIVITY_CATEGORIES:
                    for tag in connection.execute(
                        "SELECT id, name, start_date FROM custom_task_tags WHERE category = ?",
                        (task_name,),
                    ).fetchall():
                        tag_start = date.fromisoformat(tag["start_date"]) if tag["start_date"] else game_today()
                        connection.execute(
                            """
                            INSERT INTO tasks(
                                account_id, name, recurrence, next_due, sort_order, custom_tag_id, created_at
                            ) VALUES(?, ?, 'once', ?, ?, ?, ?)
                            """,
                            (account_id, tag["name"], tag_start.isoformat(),
                             _ACTIVITY_TASK_SORT_ORDER, tag["id"], now_text()),
                        )
        result = row_to_dict(
            connection.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        )
        result.pop("credentials", None)
        return result


def update_account(account_id: int, payload: dict) -> None:
    name = require_text(payload, "name")
    owner = str(payload.get("owner", "")).strip()[:100]
    notes = str(payload.get("notes", "")).strip()[:500]
    proxy_until = _parse_proxy_until(payload)
    with db_connection() as connection:
        cursor = connection.execute(
            "UPDATE accounts SET name = ?, owner = ?, notes = ?, proxy_until = ? WHERE id = ? AND active = 1",
            (name, owner, notes, proxy_until, account_id),
        )
        if cursor.rowcount == 0:
            raise LookupError("没有找到该号主")


def get_account_credentials(account_id: int) -> dict:
    with db_connection() as connection:
        row = connection.execute(
            "SELECT credentials FROM accounts WHERE id = ? AND deleted = 0", (account_id,)
        ).fetchone()
        if not row:
            raise LookupError("没有找到该号主")
        encrypted = row["credentials"]
    if not encrypted:
        return {"username": "", "password": "", "note": ""}
    try:
        decrypted = dpapi_unprotect(encrypted)
        data = json.loads(decrypted)
        return {
            "username": str(data.get("username", "")),
            "password": str(data.get("password", "")),
            "note": str(data.get("note", "")),
        }
    except Exception as error:
        raise ValueError(f"凭据解密失败：{error}") from error


def set_account_credentials(account_id: int, payload: dict) -> None:
    username = str(payload.get("username", "")).strip()[:200]
    password = str(payload.get("password", "")).strip()[:500]
    note = str(payload.get("note", "")).strip()[:200]
    with db_connection() as connection:
        account = connection.execute(
            "SELECT id FROM accounts WHERE id = ? AND deleted = 0", (account_id,)
        ).fetchone()
        if not account:
            raise LookupError("没有找到该号主")
        if not username and not password and not note:
            connection.execute("UPDATE accounts SET credentials = NULL WHERE id = ?", (account_id,))
            return
        data = json.dumps({"username": username, "password": password, "note": note}, ensure_ascii=False)
        encrypted = dpapi_protect(data)
        connection.execute("UPDATE accounts SET credentials = ? WHERE id = ?", (encrypted, account_id))


def archive_account(account_id: int) -> None:
    with db_connection() as connection:
        cursor = connection.execute(
            "UPDATE accounts SET active = 0 WHERE id = ? AND active = 1", (account_id,)
        )
        if cursor.rowcount == 0:
            raise LookupError("没有找到该号主")


def purge_account(account_id: int) -> None:
    with db_connection() as connection:
        cursor = connection.execute(
            "UPDATE accounts SET deleted = 1, active = 0 WHERE id = ? AND deleted = 0", (account_id,)
        )
        if cursor.rowcount == 0:
            raise LookupError("没有找到该号主")


def reactivate_account(account_id: int) -> None:
    with db_connection() as connection:
        cursor = connection.execute(
            "UPDATE accounts SET active = 1 WHERE id = ? AND active = 0", (account_id,)
        )
        if cursor.rowcount == 0:
            raise LookupError("没有找到该号主")


def reset_database() -> None:
    with db_connection() as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript("""
            DELETE FROM task_records;
            DELETE FROM tasks;
            DELETE FROM account_group_notes;
            DELETE FROM story_tasks;
            DELETE FROM custom_task_tags;
            DELETE FROM care_plans;
            DELETE FROM accounts;
            DELETE FROM app_meta;
        """)
        connection.execute("PRAGMA foreign_keys = ON")
    initialize_database()


def build_backup_payload() -> dict:
    with db_connection() as connection:
        return {
            "exportedAt": now_text(),
            "accounts": [
                {k: v for k, v in row_to_dict(row).items() if k != "credentials"}
                for row in connection.execute("SELECT * FROM accounts")
            ],
            "tasks": [row_to_dict(row) for row in connection.execute("SELECT * FROM tasks")],
            "records": [row_to_dict(row) for row in connection.execute("SELECT * FROM task_records")],
            "storyTasks": [row_to_dict(row) for row in connection.execute("SELECT * FROM story_tasks")],
            "customTags": [row_to_dict(row) for row in connection.execute("SELECT * FROM custom_task_tags")],
            "carePlans": [row_to_dict(row) for row in connection.execute("SELECT * FROM care_plans")],
            "groupNotes": [row_to_dict(row) for row in connection.execute("SELECT * FROM account_group_notes")],
        }


def _write_pre_import_snapshot() -> None:
    snapshot = build_backup_payload()
    if not snapshot["accounts"] and not snapshot["records"]:
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = DB_PATH.parent / f"pre-import-{stamp}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    old = sorted(DB_PATH.parent.glob("pre-import-*.json"))
    for stale in old[:-5]:
        stale.unlink(missing_ok=True)


def import_backup(payload: dict) -> None:
    if not isinstance(payload.get("accounts"), list):
        raise ValueError("备份文件格式无效，请选择由本程序导出的 JSON 文件")
    _write_pre_import_snapshot()
    with db_connection() as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        for table in ("task_records", "tasks", "account_group_notes",
                      "story_tasks", "custom_task_tags", "care_plans", "accounts"):
            connection.execute(f"DELETE FROM {table}")
        for row in payload.get("customTags", []):
            connection.execute(
                "INSERT OR IGNORE INTO custom_task_tags(id,name,category,duration_days,start_date,created_at) VALUES(?,?,?,?,?,?)",
                (row.get("id"), row.get("name"), row.get("category", "大活动"),
                 row.get("duration_days", 16), row.get("start_date"), row.get("created_at", now_text())),
            )
        for row in payload.get("carePlans", []):
            connection.execute(
                "INSERT OR IGNORE INTO care_plans(id,name,tasks,created_at) VALUES(?,?,?,?)",
                (row.get("id"), row.get("name"), row.get("tasks", "[]"), row.get("created_at", now_text())),
            )
        for row in payload.get("accounts", []):
            connection.execute(
                "INSERT OR IGNORE INTO accounts(id,name,owner,notes,proxy_until,active,deleted,sort_order,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (row.get("id"), row.get("name", ""), row.get("owner", ""), row.get("notes", ""),
                 row.get("proxy_until"), row.get("active", 1), row.get("deleted", 0),
                 row.get("sort_order", 0), row.get("created_at", now_text())),
            )
        for row in payload.get("tasks", []):
            connection.execute(
                "INSERT OR IGNORE INTO tasks(id,account_id,name,recurrence,interval_days,monthly_day,next_due,notes,active,sort_order,custom_tag_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (row.get("id"), row.get("account_id"), row.get("name", ""),
                 row.get("recurrence", "daily"), row.get("interval_days"), row.get("monthly_day"),
                 row.get("next_due"), row.get("notes", ""), row.get("active", 1),
                 row.get("sort_order", 0), row.get("custom_tag_id"), row.get("created_at", now_text())),
            )
        for row in payload.get("records", []):
            connection.execute(
                "INSERT OR IGNORE INTO task_records(id,task_id,task_date,completed_at,previous_next_due,note) VALUES(?,?,?,?,?,?)",
                (row.get("id"), row.get("task_id"), row.get("task_date", ""),
                 row.get("completed_at", now_text()), row.get("previous_next_due"), row.get("note", "")),
            )
        for row in payload.get("storyTasks", []):
            connection.execute(
                "INSERT OR IGNORE INTO story_tasks(id,account_id,owner_name,name,task_type,has_bonus,bonus_deadline,completed_at,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (row.get("id"), row.get("account_id"), row.get("owner_name", ""),
                 row.get("name", ""), row.get("task_type", "world"), row.get("has_bonus", 0),
                 row.get("bonus_deadline"), row.get("completed_at"), row.get("active", 1),
                 row.get("created_at", now_text())),
            )
        for row in payload.get("groupNotes", []):
            connection.execute(
                "INSERT OR IGNORE INTO account_group_notes(id,account_id,category,note,created_at) VALUES(?,?,?,?,?)",
                (row.get("id"), row.get("account_id"), row.get("category", "daily"),
                 row.get("note", ""), row.get("created_at", now_text())),
            )
        connection.execute("PRAGMA foreign_keys = ON")


def list_custom_tags() -> list[dict]:
    with db_connection() as connection:
        return [row_to_dict(row) for row in connection.execute("SELECT * FROM custom_task_tags ORDER BY id")]


STORY_TASK_TYPES = {"archon": "魔神任务", "legend": "传说任务", "world": "世界任务"}


def story_bonus_deadline(task_type: str, reference: datetime | None = None) -> str | None:
    if task_type == "world":
        return None
    moment = reference or datetime.now()
    with db_connection() as connection:
        anchor = get_version_anchor(connection)
    window = version_window(anchor, moment.date())
    version_start = date.fromisoformat(window["versionStart"])
    version_end = date.fromisoformat(window["eventEnd"])
    if task_type == "archon":
        deadline_day = version_end
    elif task_type == "legend":
        half_end = version_start + timedelta(days=20)
        deadline_day = half_end if moment.date() <= half_end else version_end
    else:
        raise ValueError("剧情任务类型无效")
    return f"{deadline_day.isoformat()}T15:00"


def list_story_tasks() -> list[dict]:
    with db_connection() as connection:
        return [
            row_to_dict(row)
            for row in connection.execute(
                """
                SELECT s.*,
                       COALESCE(NULLIF(s.owner_name, ''), a.name, '临时号主') AS account_name,
                       COALESCE(a.sort_order, 999999) AS account_sort_order
                FROM story_tasks s
                LEFT JOIN accounts a ON a.id = s.account_id
                WHERE s.active = 1 AND (s.account_id IS NULL OR a.deleted = 0)
                ORDER BY CASE WHEN s.completed_at IS NULL THEN 0 ELSE 1 END,
                         CASE WHEN s.bonus_deadline IS NULL THEN 1 ELSE 0 END,
                         s.bonus_deadline, account_sort_order,
                         COALESCE(a.id, 999999), s.id
                """
            )
        ]


def create_story_task(payload: dict) -> dict:
    task_type = str(payload.get("taskType", "")).strip()
    if task_type not in STORY_TASK_TYPES:
        raise ValueError("请选择剧情任务类型")
    name = str(payload.get("name", "")).strip()[:100] or STORY_TASK_TYPES[task_type]
    owner_name = str(payload.get("ownerName", "")).strip()[:100]
    try:
        account_id = int(payload.get("accountId", 0)) or None
    except (TypeError, ValueError) as error:
        raise ValueError("号主选择无效") from error
    if owner_name:
        account_id = None
    if not owner_name and not account_id:
        raise ValueError("请选择号主或填写临时号主")
    has_bonus = bool(payload.get("hasBonus")) and task_type != "world"
    deadline = story_bonus_deadline(task_type) if has_bonus else None
    with db_connection() as connection:
        if account_id:
            account = connection.execute(
                "SELECT id FROM accounts WHERE id = ? AND active = 1 AND deleted = 0",
                (account_id,),
            ).fetchone()
            if not account:
                raise ValueError("请选择有效的号主")
        cursor = connection.execute(
            """
            INSERT INTO story_tasks(
                account_id, owner_name, name, task_type, has_bonus, bonus_deadline, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (account_id, owner_name, name, task_type, int(has_bonus), deadline, now_text()),
        )
        return row_to_dict(
            connection.execute(
                "SELECT * FROM story_tasks WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        )


def toggle_story_task(task_id: int, completed: bool) -> None:
    with db_connection() as connection:
        cursor = connection.execute(
            "UPDATE story_tasks SET completed_at = ? WHERE id = ? AND active = 1",
            (now_text() if completed else None, task_id),
        )
        if cursor.rowcount == 0:
            raise LookupError("没有找到该剧情任务")


def archive_story_task(task_id: int) -> None:
    with db_connection() as connection:
        cursor = connection.execute(
            "UPDATE story_tasks SET active = 0 WHERE id = ? AND active = 1", (task_id,)
        )
        if cursor.rowcount == 0:
            raise LookupError("没有找到该剧情任务")


_VALID_DURATIONS = {"大活动": (16, 23), "小活动": (7, 10)}

def create_custom_tag(payload: dict) -> dict:
    name = require_text(payload, "name")
    if name in RESERVED_ACTIVITY_NAMES:
        raise ValueError("活动名称不能与内置任务重名")
    category = str(payload.get("category", "")).strip()
    if category not in _VALID_DURATIONS:
        raise ValueError(f"活动类型无效，应为：{', '.join(_VALID_DURATIONS)}")
    duration_days = int(payload.get("durationDays", 0))
    if not (1 <= duration_days <= 365):
        raise ValueError("活动时长无效，应为 1 到 365 天")
    raw_start = str(payload.get("startDate", "")).strip()
    try:
        start_date = date.fromisoformat(raw_start).isoformat() if raw_start else game_today().isoformat()
    except ValueError:
        raise ValueError("开始日期格式无效")
    with db_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO custom_task_tags(name, category, duration_days, start_date, created_at) VALUES(?, ?, ?, ?, ?)",
            (name, category, duration_days, start_date, now_text()),
        )
        return row_to_dict(
            connection.execute("SELECT * FROM custom_task_tags WHERE id = ?", (cursor.lastrowid,)).fetchone()
        )


def delete_custom_tag(tag_id: int) -> None:
    with db_connection() as connection:
        tag = connection.execute("SELECT name FROM custom_task_tags WHERE id = ?", (tag_id,)).fetchone()
        if not tag:
            raise LookupError("没有找到该任务标签")
        connection.execute(
            "UPDATE tasks SET active = 0 WHERE custom_tag_id = ? AND active = 1",
            (tag_id,),
        )
        connection.execute("DELETE FROM custom_task_tags WHERE id = ?", (tag_id,))


def set_account_custom_tag(account_id: int, payload: dict) -> None:
    try:
        tag_id = int(payload.get("tagId", 0))
    except (TypeError, ValueError) as error:
        raise ValueError("活动标签格式无效") from error
    if tag_id <= 0:
        raise ValueError("请选择有效的活动标签")
    enabled = bool(payload.get("enabled"))
    with db_connection() as connection:
        tag = connection.execute(
            "SELECT id, name, duration_days, start_date FROM custom_task_tags WHERE id = ?", (tag_id,)
        ).fetchone()
        if not tag:
            raise ValueError("自定义任务标签不存在")
        account = connection.execute(
            "SELECT id FROM accounts WHERE id = ? AND active = 1", (account_id,)
        ).fetchone()
        if not account:
            raise LookupError("没有找到该号主")
        active_task = connection.execute(
            "SELECT id FROM tasks WHERE account_id = ? AND custom_tag_id = ? AND active = 1 ORDER BY id DESC LIMIT 1",
            (account_id, tag_id),
        ).fetchone()
        if not enabled:
            if active_task:
                connection.execute("UPDATE tasks SET active = 0 WHERE id = ?", (active_task["id"],))
            return
        if active_task:
            return
        tag_start = date.fromisoformat(tag["start_date"]) if tag["start_date"] else game_today()
        next_due = tag_start.isoformat()
        connection.execute(
            """
            INSERT INTO tasks(
                account_id, name, recurrence, next_due, sort_order, custom_tag_id, created_at
            ) VALUES(?, ?, 'once', ?, ?, ?, ?)
            """,
            (account_id, tag["name"], next_due, _ACTIVITY_TASK_SORT_ORDER, tag_id, now_text()),
        )


def enable_custom_tag_for_all(tag_id: int) -> int:
    with db_connection() as connection:
        tag = connection.execute(
            "SELECT id, name, start_date FROM custom_task_tags WHERE id = ?", (tag_id,)
        ).fetchone()
        if not tag:
            raise ValueError("自定义任务标签不存在")
        tag_start = date.fromisoformat(tag["start_date"]) if tag["start_date"] else game_today()
        next_due = tag_start.isoformat()
        enabled_count = 0
        account_ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM accounts WHERE active = 1 AND deleted = 0"
            )
        ]
        for account_id in account_ids:
            existing = connection.execute(
                "SELECT id FROM tasks WHERE account_id = ? AND custom_tag_id = ? AND active = 1 LIMIT 1",
                (account_id, tag_id),
            ).fetchone()
            if existing:
                continue
            connection.execute(
                """
                INSERT INTO tasks(
                    account_id, name, recurrence, next_due, sort_order, custom_tag_id, created_at
                ) VALUES(?, ?, 'once', ?, ?, ?, ?)
                """,
                (account_id, tag["name"], next_due, _ACTIVITY_TASK_SORT_ORDER, tag_id, now_text()),
            )
            enabled_count += 1
        return enabled_count


def reorder_accounts(payload: dict) -> None:
    raw_ids = payload.get("accountIds", [])
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError("号主顺序不能为空")
    try:
        account_ids = [int(value) for value in raw_ids]
    except (TypeError, ValueError) as error:
        raise ValueError("号主顺序格式无效") from error
    if len(account_ids) != len(set(account_ids)):
        raise ValueError("号主顺序中存在重复项")

    with db_connection() as connection:
        active_ids = {
            row[0] for row in connection.execute(
                "SELECT id FROM accounts WHERE active = 1"
            )
        }
        if set(account_ids) != active_ids:
            raise ValueError("号主顺序与当前名单不一致，请刷新后重试")
        connection.executemany(
            "UPDATE accounts SET sort_order = ? WHERE id = ?",
            [(sort_order, account_id) for sort_order, account_id in enumerate(account_ids)],
        )


def create_task(payload: dict) -> dict:
    account_id = int(payload.get("accountId", 0))
    name = require_text(payload, "name")
    recurrence = str(payload.get("recurrence", "once"))
    if recurrence not in {"daily", "weekly", "interval", "once", "monthly", "version"}:
        raise ValueError("任务类型无效")
    interval_days = None
    monthly_day = None
    if recurrence == "interval":
        interval_days = int(payload.get("intervalDays", 0))
        if interval_days < 1 or interval_days > 365:
            raise ValueError("间隔天数需要在 1 到 365 之间")
    next_due = optional_date(payload.get("nextDue"))
    if recurrence in {"interval", "once"} and not next_due:
        raise ValueError("周期任务和临时任务需要填写到期日期")
    if recurrence == "monthly":
        monthly_day = int(payload.get("monthlyDay", 0))
        if monthly_day < 1 or monthly_day > 28:
            raise ValueError("每月刷新日期需要在 1 到 28 日之间")
        next_due = monthly_occurrence(game_today(), monthly_day).isoformat()
    if recurrence == "version":
        settings = get_schedule_settings()
        next_due = settings["warWindow"]["eventStart"] if settings["warWindow"] else None
    notes = str(payload.get("notes", "")).strip()[:500]
    with db_connection() as connection:
        account = connection.execute(
            "SELECT id FROM accounts WHERE id = ? AND active = 1", (account_id,)
        ).fetchone()
        if not account:
            raise ValueError("请选择有效的号主")
        cursor = connection.execute(
            """
            INSERT INTO tasks(account_id, name, recurrence, interval_days, monthly_day, next_due, notes, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (account_id, name, recurrence, interval_days, monthly_day, next_due, notes, now_text()),
        )
        return row_to_dict(
            connection.execute("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)).fetchone()
        )


def update_task(task_id: int, payload: dict) -> None:
    name = require_text(payload, "name")
    recurrence = str(payload.get("recurrence", "once"))
    if recurrence not in {"daily", "weekly", "interval", "once", "monthly", "version"}:
        raise ValueError("任务类型无效")
    interval_days = None
    monthly_day = None
    if recurrence == "interval":
        interval_days = int(payload.get("intervalDays", 0))
        if interval_days < 1 or interval_days > 365:
            raise ValueError("间隔天数需要在 1 到 365 之间")
    next_due = optional_date(payload.get("nextDue"))
    if recurrence in {"interval", "once"} and not next_due:
        raise ValueError("周期任务和临时任务需要填写到期日期")
    if recurrence == "monthly":
        monthly_day = int(payload.get("monthlyDay", 0))
        if monthly_day < 1 or monthly_day > 28:
            raise ValueError("每月刷新日期需要在 1 到 28 日之间")
        next_due = monthly_occurrence(game_today(), monthly_day).isoformat()
    if recurrence == "version":
        settings = get_schedule_settings()
        next_due = settings["warWindow"]["eventStart"] if settings["warWindow"] else None
    notes = str(payload.get("notes", "")).strip()[:500]
    with db_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE tasks
            SET name = ?, recurrence = ?, interval_days = ?, monthly_day = ?, next_due = ?, notes = ?
            WHERE id = ? AND active = 1
            """,
            (name, recurrence, interval_days, monthly_day, next_due, notes, task_id),
        )
        if cursor.rowcount == 0:
            raise LookupError("没有找到该任务")


def archive_task(task_id: int) -> None:
    with db_connection() as connection:
        cursor = connection.execute(
            "UPDATE tasks SET active = 0 WHERE id = ? AND active = 1", (task_id,)
        )
        if cursor.rowcount == 0:
            raise LookupError("没有找到该任务")


def set_account_task_tag(account_id: int, payload: dict) -> None:
    task_name = require_text(payload, "tag")
    if task_name not in TASK_TAG_PRESETS:
        raise ValueError("任务标签无效")
    enabled = bool(payload.get("enabled"))
    preset = TASK_TAG_PRESETS[task_name]
    notes = normalize_task_notes(payload["notes"]) if "notes" in payload else None

    with db_connection() as connection:
        account = connection.execute(
            "SELECT id FROM accounts WHERE id = ? AND active = 1", (account_id,)
        ).fetchone()
        if not account:
            raise LookupError("没有找到该号主")

        active_task = connection.execute(
            """
            SELECT id FROM tasks
            WHERE account_id = ? AND name = ? AND active = 1
            ORDER BY id DESC LIMIT 1
            """,
            (account_id, task_name),
        ).fetchone()
        if not enabled:
            if active_task:
                connection.execute("UPDATE tasks SET active = 0 WHERE id = ?", (active_task["id"],))
            return
        if active_task:
            if notes is not None:
                connection.execute(
                    "UPDATE tasks SET notes = ? WHERE id = ?",
                    (notes, active_task["id"]),
                )
            return

        _insert_preset_task(connection, account_id, task_name, notes)


def _insert_preset_task(connection: sqlite3.Connection, account_id: int, task_name: str, notes: str | None = None) -> None:
    preset = TASK_TAG_PRESETS[task_name]
    recurrence = preset["recurrence"]
    interval_days = preset.get("interval_days")
    monthly_day = preset.get("monthly_day")
    next_due = None
    if recurrence == "interval":
        next_due = game_today().isoformat()
    elif recurrence == "monthly":
        next_due = monthly_occurrence(game_today(), monthly_day).isoformat()
    elif recurrence == "version":
        version_anchor = get_version_anchor(connection)
        window = version_window(version_anchor)
        next_due = window["eventStart"] if window else None

    preset_order = _TASK_SORT_ORDER.get(task_name, 0)
    connection.execute(
        """
        INSERT INTO tasks(
            account_id, name, recurrence, interval_days, monthly_day, next_due, notes, sort_order, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            account_id, task_name, recurrence, interval_days, monthly_day,
            next_due, notes or "", preset_order, now_text(),
        ),
    )


def normalize_task_notes(raw_notes) -> str:
    if not isinstance(raw_notes, list):
        raise ValueError("备注格式无效")

    notes = []
    for value in raw_notes[:20]:
        note = str(value).strip()[:40]
        if note and note not in notes:
            notes.append(note)
    joined = "、".join(notes)
    if len(joined) > 500:
        raise ValueError("备注内容过长")
    return joined


def set_task_notes(task_id: int, payload: dict) -> None:
    joined = normalize_task_notes(payload.get("notes", []))

    with db_connection() as connection:
        task = connection.execute(
            "SELECT id FROM tasks WHERE id = ? AND active = 1", (task_id,)
        ).fetchone()
        if not task:
            raise LookupError("没有找到该任务")
        connection.execute(
            "UPDATE tasks SET notes = ? WHERE id = ?", (joined, task_id)
        )


def toggle_task(task_id: int, task_date: str, completed: bool, used_at=None, connection=None, restart_cycle=False) -> None:
    parsed_task_date = date.fromisoformat(task_date)
    with (db_connection() if connection is None else nullcontext(connection)) as connection:
        task = connection.execute(
            "SELECT * FROM tasks WHERE id = ? AND active = 1", (task_id,)
        ).fetchone()
        if not task:
            raise LookupError("没有找到该任务")
        if task["recurrence"] == "weekly":
            cycle_key = weekly_cycle_key(parsed_task_date)
            existing = connection.execute(
                "SELECT * FROM task_records WHERE task_id = ? AND note = ?",
                (task_id, cycle_key),
            ).fetchone()
        else:
            cycle_key = ""
            existing = connection.execute(
                "SELECT * FROM task_records WHERE task_id = ? AND task_date = ? AND note = ''",
                (task_id, task_date),
            ).fetchone()

        if completed and not existing:
            previous_due = task["next_due"]
            precise_used_at = None
            if task["name"] in ("质变仪", "探索派遣") and task["recurrence"] == "interval":
                precise_used_at = optional_local_datetime(used_at) or datetime.now().replace(
                    second=0, microsecond=0
                )
            connection.execute(
                """
                INSERT INTO task_records(task_id, task_date, completed_at, previous_next_due, note)
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    task_date,
                    precise_used_at.isoformat(timespec="seconds") if precise_used_at else now_text(),
                    previous_due,
                    cycle_key,
                ),
            )
            if task["recurrence"] == "interval":
                if precise_used_at and task["name"] == "质变仪":
                    next_due = precise_used_at + timedelta(hours=24 * task["interval_days"])
                    next_due_text = next_due.isoformat(timespec="minutes")
                elif precise_used_at and task["name"] == "探索派遣":
                    hours = _expedition_hours_from_notes(task["notes"])
                    next_due = precise_used_at + timedelta(hours=hours)
                    next_due_text = next_due.isoformat(timespec="minutes")
                else:
                    if not task["interval_days"]:
                        raise ValueError("任务冷却天数未配置，请检查任务设置")
                    if restart_cycle:
                        base_date = date.fromisoformat(task_date)
                    else:
                        prev_date = date.fromisoformat(previous_due) if previous_due else date.fromisoformat(task_date)
                        base_date = max(prev_date, date.fromisoformat(task_date))
                    next_due_text = (base_date + timedelta(days=task["interval_days"])).isoformat()
                connection.execute(
                    "UPDATE tasks SET next_due = ? WHERE id = ?",
                    (next_due_text, task_id),
                )
            elif task["recurrence"] == "monthly":
                prev_date = date.fromisoformat(previous_due) if previous_due else date.fromisoformat(task_date)
                base_date = max(prev_date, date.fromisoformat(task_date))
                if base_date.day < task["monthly_day"]:
                    next_due = monthly_occurrence(base_date, task["monthly_day"])
                else:
                    next_due = next_month_occurrence(base_date, task["monthly_day"])
                connection.execute(
                    "UPDATE tasks SET next_due = ? WHERE id = ?",
                    (next_due.isoformat(), task_id),
                )
            elif task["recurrence"] == "version":
                connection.execute("UPDATE tasks SET next_due = NULL WHERE id = ?", (task_id,))
        elif not completed and existing:
            if task["recurrence"] in {"interval", "monthly", "version"}:
                connection.execute(
                    "UPDATE tasks SET next_due = ? WHERE id = ?",
                    (existing["previous_next_due"], task_id),
                )
            connection.execute("DELETE FROM task_records WHERE id = ?", (existing["id"],))


def complete_all(task_date: str, task_ids: list[int]) -> None:
    with db_connection() as connection:
        for task_id in task_ids:
            toggle_task(int(task_id), task_date, True, connection=connection)


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "TaskRecorder/1.0"

    def log_message(self, format_string: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {format_string % args}")

    def send_json(self, data, status: int = HTTPStatus.OK) -> None:
        body = json.dumps({"success": True, "data": data}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status: int) -> None:
        body = json.dumps({"success": False, "error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        port = self.server.server_address[1]
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self.send_error(HTTPStatus.FORBIDDEN)
            return

        if method in ("POST", "PUT", "DELETE"):
            origin = self.headers.get("Origin", "")
            if origin:
                if origin != f"http://127.0.0.1:{port}":
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return

        if path.startswith("/api/"):
            try:
                self.handle_api(method, path, query)
            except (ValueError, sqlite3.IntegrityError) as error:
                message = "名称已存在" if isinstance(error, sqlite3.IntegrityError) else str(error)
                self.send_error_json(message, HTTPStatus.BAD_REQUEST)
            except LookupError as error:
                self.send_error_json(str(error), HTTPStatus.NOT_FOUND)
            except Exception as error:
                print(f"API error: {error!r}")
                self.send_error_json("操作失败，请稍后重试", HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if method != "GET":
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
            return
        self.serve_static(path)

    def handle_api(self, method: str, path: str, query: dict) -> None:
        if method == "GET" and path == "/api/heartbeat":
            global _last_heartbeat
            _last_heartbeat = time.time()
            self.send_json(None)
            return
        if method == "GET" and path == "/api/state":
            selected_date = query.get("date", [today_text()])[0]
            self.send_json(load_state(selected_date))
            return
        if method == "GET" and path == "/api/history":
            end_date = query.get("end", [today_text()])[0]
            start_date = query.get("start", [""])[0] or "0001-01-01"
            account_id_str = query.get("accountId", [None])[0]
            account_id = int(account_id_str) if account_id_str else None
            self.send_json(list_history(start_date, end_date, account_id))
            return
        if method == "GET" and path == "/api/settings":
            self.send_json(get_schedule_settings())
            return
        if method == "GET" and path == "/api/update/check":
            self.send_json(check_for_update())
            return
        if method == "GET" and path == "/api/update/progress":
            self.send_json(dict(_update_state))
            return
        if method == "GET" and path == "/api/export":
            self.send_json(build_backup_payload())
            return
        if method == "POST" and path == "/api/import":
            length = int(self.headers.get("Content-Length", "0"))
            if length > 50_000_000:
                raise ValueError("文件过大（最大 50 MB）")
            raw = self.rfile.read(length)
            import_backup(json.loads(raw.decode("utf-8")) if raw else {})
            self.send_json(None)
            return
        payload = self.read_json()
        if method == "PUT" and path == "/api/settings/version":
            self.send_json(update_version_start(payload))
            return
        if method == "POST" and path == "/api/accounts":
            self.send_json(create_account(payload), HTTPStatus.CREATED)
            return
        match = re.fullmatch(r"/api/accounts/(\d+)", path)
        if match and method == "PUT":
            update_account(int(match.group(1)), payload)
            self.send_json(None)
            return
        if match and method == "DELETE":
            archive_account(int(match.group(1)))
            self.send_json(None)
            return
        match = re.fullmatch(r"/api/accounts/(\d+)/reactivate", path)
        if match and method == "POST":
            reactivate_account(int(match.group(1)))
            self.send_json(None)
            return
        match = re.fullmatch(r"/api/accounts/(\d+)/purge", path)
        if match and method == "POST":
            purge_account(int(match.group(1)))
            self.send_json(None)
            return
        match = re.fullmatch(r"/api/accounts/(\d+)/task-tags", path)
        if match and method == "POST":
            set_account_task_tag(int(match.group(1)), payload)
            self.send_json(None)
            return
        match = re.fullmatch(r"/api/accounts/(\d+)/credentials", path)
        if match and method == "GET":
            self.send_json(get_account_credentials(int(match.group(1))))
            return
        if match and method == "PUT":
            set_account_credentials(int(match.group(1)), payload)
            self.send_json(None)
            return
        if match and method == "DELETE":
            set_account_credentials(int(match.group(1)), {})
            self.send_json(None)
            return
        if method == "POST" and path == "/api/accounts/reorder":
            reorder_accounts(payload)
            self.send_json(None)
            return
        if method == "POST" and path == "/api/tasks":
            self.send_json(create_task(payload), HTTPStatus.CREATED)
            return
        match = re.fullmatch(r"/api/tasks/(\d+)", path)
        if match and method == "PUT":
            update_task(int(match.group(1)), payload)
            self.send_json(None)
            return
        if match and method == "DELETE":
            archive_task(int(match.group(1)))
            self.send_json(None)
            return
        match = re.fullmatch(r"/api/tasks/(\d+)/notes", path)
        if match and method == "POST":
            set_task_notes(int(match.group(1)), payload)
            self.send_json(None)
            return
        match = re.fullmatch(r"/api/tasks/(\d+)/toggle", path)
        if match and method == "POST":
            toggle_task(
                int(match.group(1)),
                require_text(payload, "date", 10),
                bool(payload.get("completed")),
                payload.get("usedAt"),
                restart_cycle=bool(payload.get("restartCycle")),
            )
            self.send_json(None)
            return
        if method == "POST" and path == "/api/tasks/complete-all":
            task_date = require_text(payload, "date", 10)
            task_ids = payload.get("taskIds", [])
            if not isinstance(task_ids, list):
                raise ValueError("任务列表格式无效")
            complete_all(task_date, task_ids)
            self.send_json(None)
            return
        if method == "POST" and path == "/api/story-tasks":
            self.send_json(create_story_task(payload), HTTPStatus.CREATED)
            return
        match = re.fullmatch(r"/api/story-tasks/(\d+)/toggle", path)
        if match and method == "POST":
            toggle_story_task(int(match.group(1)), bool(payload.get("completed")))
            self.send_json(None)
            return
        match = re.fullmatch(r"/api/story-tasks/(\d+)", path)
        if match and method == "DELETE":
            archive_story_task(int(match.group(1)))
            self.send_json(None)
            return
        if method == "GET" and path == "/api/custom-tags":
            self.send_json(list_custom_tags())
            return
        if method == "POST" and path == "/api/custom-tags":
            self.send_json(create_custom_tag(payload), HTTPStatus.CREATED)
            return
        match = re.fullmatch(r"/api/custom-tags/(\d+)", path)
        if match and method == "DELETE":
            delete_custom_tag(int(match.group(1)))
            self.send_json(None)
            return
        match = re.fullmatch(r"/api/accounts/(\d+)/custom-tags", path)
        if match and method == "POST":
            set_account_custom_tag(int(match.group(1)), payload)
            self.send_json(None)
            return
        match = re.fullmatch(r"/api/custom-tags/(\d+)/enable-all", path)
        if match and method == "POST":
            self.send_json({"enabled": enable_custom_tag_for_all(int(match.group(1)))})
            return
        if method == "POST" and path == "/api/care-plans":
            self.send_json(create_care_plan(payload), HTTPStatus.CREATED)
            return
        match = re.fullmatch(r"/api/care-plans/(\d+)", path)
        if match and method == "PUT":
            update_care_plan(int(match.group(1)), payload)
            self.send_json(None)
            return
        if match and method == "DELETE":
            delete_care_plan(int(match.group(1)))
            self.send_json(None)
            return
        if method == "POST" and path == "/api/update/apply":
            start_update()
            self.send_json(None)
            return
        if method == "POST" and path == "/api/reset":
            reset_database()
            self.send_json(None)
            return
        if method == "POST" and path == "/api/shutdown":
            self.send_json(None)
            def stop_process() -> None:
                time.sleep(0.2)
                os._exit(0)

            threading.Thread(target=stop_process, daemon=True).start()
            return
        self.send_error_json("接口不存在", HTTPStatus.NOT_FOUND)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        file_path = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in file_path.parents and file_path != STATIC_DIR.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.dispatch("GET")

    def do_POST(self) -> None:
        self.dispatch("POST")

    def do_PUT(self) -> None:
        self.dispatch("PUT")

    def do_DELETE(self) -> None:
        self.dispatch("DELETE")


def create_http_server(port: int) -> tuple[ExclusiveThreadingHTTPServer, int]:
    try:
        server = ExclusiveThreadingHTTPServer(("127.0.0.1", port), RequestHandler)
    except OSError as error:
        recoverable_errors = {errno.EACCES, errno.EADDRINUSE}
        if error.errno not in recoverable_errors and getattr(error, "winerror", None) not in {10013, 10048}:
            raise
        print(f"Port {port} is unavailable; selecting another local port.", flush=True)
        server = ExclusiveThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
    return server, int(server.server_address[1])


def heartbeat_watchdog() -> None:
    while True:
        time.sleep(5)
        if _update_state["status"] == "downloading":
            continue
        if _last_heartbeat and time.time() - _last_heartbeat > HEARTBEAT_TIMEOUT:
            print("心跳超时，程序自动退出。", flush=True)
            os._exit(0)


def _webview_storage_path() -> str:
    path = DATA_DIR / "webview"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def launch_window(url: str) -> bool:
    """在原生窗口中打开界面；WebView2 不可用时返回 False 以便回退浏览器。"""
    try:
        import webview
    except Exception as error:
        print(f"原生窗口组件不可用（{error!r}），回退到浏览器模式。", flush=True)
        return False
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.create_window(
            "LeyLineBook / 地脉簿",
            url,
            width=1440,
            height=920,
            min_size=(1080, 680),
        )
        webview.start(private_mode=False, storage_path=_webview_storage_path())
        return True
    except Exception as error:
        print(f"原生窗口启动失败（{error!r}），回退到浏览器模式。", flush=True)
        return False


def run_server(port: int, mode: str) -> None:
    if sys.stdout is None:
        sys.stdout = LOG_PATH.open("a", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = LOG_PATH.open("a", encoding="utf-8")
    initialize_database()
    server, port = create_http_server(port)
    url = f"http://127.0.0.1:{port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"LeyLineBook / 地脉簿已启动：{url}（模式：{mode}）")
    sys.stdout.flush()

    if mode == "window":
        if launch_window(url):
            print("窗口已关闭，程序退出。", flush=True)
            server.shutdown()
            server.server_close()
            return
        mode = "browser"

    global _last_heartbeat
    _last_heartbeat = time.time()
    threading.Thread(target=heartbeat_watchdog, daemon=True).start()
    if mode == "browser":
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def is_already_running(port: int) -> bool:
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1)
        return True
    except Exception:
        return False


def stop_running_server(port: int) -> None:
    import urllib.request

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/shutdown",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(request, timeout=2).read()
    for _ in range(20):
        time.sleep(0.1)
        if not is_already_running(port):
            return
    raise RuntimeError("旧版本未能正常关闭，请稍后重试")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LeyLineBook / 地脉簿")
    parser.add_argument("--port", type=int, default=int(os.environ.get("TASK_RECORDER_PORT", "8765")))
    parser.add_argument("--browser", action="store_true", help="使用系统浏览器打开界面（旧模式）")
    parser.add_argument("--no-browser", action="store_true", help="仅启动后台服务，不打开界面")
    arguments = parser.parse_args()
    if arguments.no_browser:
        startup_mode = "headless"
    elif arguments.browser:
        startup_mode = "browser"
    else:
        startup_mode = "window"
    if is_already_running(arguments.port):
        stop_running_server(arguments.port)
    run_server(arguments.port, startup_mode)
