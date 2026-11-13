import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from app.config import Config

SEED_PIPELINES = [
    ("订单明细日抽", "orders_daily", "待抽取", "mock://orders/day"),
    ("用户画像宽表", "user_profile", "待抽取", "mock://users/profile"),
    ("库存快照落地", "inventory_snap", "待抽取", "mock://stock/snap"),
    ("支付流水清洗", "pay_clean", "待抽取", "mock://pay/flow"),
]

DEFAULT_DAILY_QUOTA = 50

# 超额路径：只能选一种写进说明（超额设置），当日禁止改口。
OVERFLOW_PATHS = {
    "pool": "超额进候补池·次日再落",
    "fail": "超额当场失败·配额已满",
}

# 单进程内串行化所有读写，保证“两人同时争抢最后一个名额只成一笔”。
DB_LOCK = threading.RLock()


class BizError(Exception):
    """业务规则拦截，消息原样回给前端。"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    # 整个请求持锁，扣减-判断-提交不被并发打断。
    with DB_LOCK:
        conn = connect(db_path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _migrate_pipelines(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pipelines)")}
    if "daily_quota" not in cols:
        conn.execute(
            "ALTER TABLE pipelines ADD COLUMN daily_quota INTEGER NOT NULL "
            f"DEFAULT {DEFAULT_DAILY_QUOTA}"
        )
    if "overflow_path" not in cols:
        conn.execute("ALTER TABLE pipelines ADD COLUMN overflow_path TEXT")
    if "overflow_path_day" not in cols:
        conn.execute("ALTER TABLE pipelines ADD COLUMN overflow_path_day TEXT")


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pipelines (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              code TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL,
              source_uri TEXT NOT NULL,
              extracted INTEGER NOT NULL DEFAULT 0,
              loaded INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quota_ledger (
              pipeline_id INTEGER NOT NULL,
              settle_day TEXT NOT NULL,
              loaded INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (pipeline_id, settle_day)
            );
            CREATE TABLE IF NOT EXISTS candidates (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              pipeline_id INTEGER NOT NULL,
              seq INTEGER NOT NULL,
              rows_pending INTEGER NOT NULL,
              created_day TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'waiting'
            );
            CREATE TABLE IF NOT EXISTS quota_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              pipeline_id INTEGER NOT NULL,
              settle_day TEXT NOT NULL,
              kind TEXT NOT NULL,
              amount INTEGER NOT NULL DEFAULT 0,
              remaining_after INTEGER,
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        _migrate_pipelines(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM pipelines").fetchone()["c"]
        if n == 0:
            conn.executemany(
                "INSERT INTO pipelines (title, code, status, source_uri) VALUES (?,?,?,?)",
                SEED_PIPELINES,
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('lab_name', ?)",
                (Config.LAB_NAME,),
            )


# ---------- 读取与序列化 ----------


def _ledger_loaded(conn: sqlite3.Connection, pipeline_id: int, day: str) -> int:
    row = conn.execute(
        "SELECT loaded FROM quota_ledger WHERE pipeline_id = ? AND settle_day = ?",
        (pipeline_id, day),
    ).fetchone()
    return row["loaded"] if row else 0


def _pending_rows(conn: sqlite3.Connection, pipeline_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(rows_pending), 0) AS s FROM candidates "
        "WHERE pipeline_id = ? AND status = 'waiting'",
        (pipeline_id,),
    ).fetchone()
    return row["s"]


def _path_name(path: str | None) -> str:
    return OVERFLOW_PATHS.get(path) or "未设置·默认当场失败"


def _enrich(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    item = dict(row)
    day = Config.settle_day()
    today = _ledger_loaded(conn, item["id"], day)
    item["settle_day"] = day
    item["today_loaded"] = today
    item["remaining"] = max(item["daily_quota"] - today, 0)
    item["candidates_pending_rows"] = _pending_rows(conn, item["id"])
    item["overflow_path_name"] = _path_name(item["overflow_path"])
    item["path_locked_today"] = bool(
        item["overflow_path"] and item["overflow_path_day"] == day
    )
    return item


def list_pipelines(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, title, code, status, source_uri, extracted, loaded, created_at, "
        "daily_quota, overflow_path, overflow_path_day "
        "FROM pipelines ORDER BY id"
    ).fetchall()
    return [_enrich(conn, r) for r in rows]


def get_pipeline(conn: sqlite3.Connection, pipeline_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, title, code, status, source_uri, extracted, loaded, created_at, "
        "daily_quota, overflow_path, overflow_path_day "
        "FROM pipelines WHERE id = ?",
        (pipeline_id,),
    ).fetchone()
    if not row:
        return None
    item = _enrich(conn, row)
    item["candidates"] = [
        dict(r)
        for r in conn.execute(
            "SELECT id, seq, rows_pending, created_day, status FROM candidates "
            "WHERE pipeline_id = ? ORDER BY seq",
            (pipeline_id,),
        ).fetchall()
    ]
    item["events"] = [
        dict(r)
        for r in conn.execute(
            "SELECT id, settle_day, kind, amount, remaining_after, note, created_at "
            "FROM quota_events WHERE pipeline_id = ? ORDER BY id DESC LIMIT 50",
            (pipeline_id,),
        ).fetchall()
    ]
    return item


def quota_board(conn: sqlite3.Connection) -> dict:
    items = list_pipelines(conn)
    return {
        "settle_day": Config.settle_day(),
        "totals": {
            "pipelines": len(items),
            "quota": sum(i["daily_quota"] for i in items),
            "loaded": sum(i["today_loaded"] for i in items),
            "candidate_rows": sum(i["candidates_pending_rows"] for i in items),
        },
        "items": items,
    }


# ---------- 记账 ----------


def _add_event(
    conn: sqlite3.Connection,
    pipeline_id: int,
    day: str,
    kind: str,
    amount: int,
    remaining_after: int | None,
    note: str = "",
) -> None:
    conn.execute(
        "INSERT INTO quota_events (pipeline_id, settle_day, kind, amount, "
        "remaining_after, note) VALUES (?,?,?,?,?,?)",
        (pipeline_id, day, kind, amount, remaining_after, note),
    )


def _deduct(
    conn: sqlite3.Connection, pipeline_id: int, day: str, amount: int
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO quota_ledger (pipeline_id, settle_day, loaded) "
        "VALUES (?,?,0)",
        (pipeline_id, day),
    )
    conn.execute(
        "UPDATE quota_ledger SET loaded = loaded + ? "
        "WHERE pipeline_id = ? AND settle_day = ?",
        (amount, pipeline_id, day),
    )


# ---------- 动作 ----------


def run_extract(conn: sqlite3.Connection, pipeline_id: int) -> dict | None:
    item = get_pipeline(conn, pipeline_id)
    if not item:
        return None
    if item["status"] == "已抽取待加载":
        return item
    # 已落库/部分落库后可开新一轮抽取；抽取条数不看配额。
    extracted = 12 + (pipeline_id % 5)
    conn.execute(
        "UPDATE pipelines SET status = ?, extracted = ?, loaded = 0 WHERE id = ?",
        ("已抽取待加载", extracted, pipeline_id),
    )
    return get_pipeline(conn, pipeline_id)


def run_load(
    conn: sqlite3.Connection, pipeline_id: int, requested_path: str | None = None
) -> dict | None:
    item = get_pipeline(conn, pipeline_id)
    if not item:
        return None
    day = Config.settle_day()
    status = item["status"]
    if status in ("待抽取", "抽取失败"):
        raise BizError("管道还没抽完，不能落库")
    if status != "已抽取待加载":
        raise BizError("本轮已落库，重新抽取后再落")

    recorded = item["overflow_path"] or "fail"
    if requested_path and requested_path != recorded:
        _add_event(
            conn, pipeline_id, day, "路径写歪", 0, item["remaining"],
            f"请求路径 {requested_path} ≠ 说明 {recorded}",
        )
        conn.commit()  # 拦截也留痕
        raise BizError("超额路径写歪了")

    needed = item["extracted"]
    loaded_today = _ledger_loaded(conn, pipeline_id, day)
    remaining = item["daily_quota"] - loaded_today

    if needed <= remaining:
        granted, overflow = needed, 0
    elif recorded == "fail":
        _add_event(
            conn, pipeline_id, day, "配额已满", needed, max(remaining, 0),
            "超额当场失败",
        )
        conn.commit()  # 配额已满写进抢占记录
        raise BizError("配额已满")
    else:
        granted = max(remaining, 0)
        overflow = needed - granted

    if granted:
        _deduct(conn, pipeline_id, day, granted)
        _add_event(
            conn, pipeline_id, day, "扣减", granted, remaining - granted, "落库扣减"
        )
    if overflow:
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM candidates WHERE pipeline_id = ?",
            (pipeline_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO candidates (pipeline_id, seq, rows_pending, created_day, "
            "status) VALUES (?,?,?,?, 'waiting')",
            (pipeline_id, seq_row["n"], overflow, day),
        )
        _add_event(
            conn, pipeline_id, day, "候补入账", overflow, remaining - granted,
            "超额进候补池，留到次日再落",
        )

    new_status = "已落库" if overflow == 0 else "部分落库"
    conn.execute(
        "UPDATE pipelines SET status = ?, loaded = ? WHERE id = ?",
        (new_status, granted, pipeline_id),
    )
    return get_pipeline(conn, pipeline_id)


def set_quota(
    conn: sqlite3.Connection,
    pipeline_id: int,
    daily_quota: int | None = None,
    overflow_path: str | None = None,
) -> dict | None:
    item = get_pipeline(conn, pipeline_id)
    if not item:
        return None
    if daily_quota is None and overflow_path is None:
        raise BizError("缺少设置项")
    day = Config.settle_day()
    if overflow_path is not None:
        if overflow_path not in OVERFLOW_PATHS:
            raise BizError("未知超额路径")
        cur, cur_day = item["overflow_path"], item["overflow_path_day"]
        if cur and cur_day == day and overflow_path != cur:
            raise BizError("当日禁止改口：超额路径次日再改")
        if overflow_path != cur or cur_day != day:
            conn.execute(
                "UPDATE pipelines SET overflow_path = ?, overflow_path_day = ? "
                "WHERE id = ?",
                (overflow_path, day, pipeline_id),
            )
    if daily_quota is not None:
        if isinstance(daily_quota, bool) or not isinstance(daily_quota, int):
            raise BizError("配额必须是非负整数")
        if daily_quota < 0:
            raise BizError("配额必须是非负整数")
        conn.execute(
            "UPDATE pipelines SET daily_quota = ? WHERE id = ?",
            (daily_quota, pipeline_id),
        )
    return get_pipeline(conn, pipeline_id)


def continue_candidates(conn: sqlite3.Connection, pipeline_id: int) -> dict | None:
    item = get_pipeline(conn, pipeline_id)
    if not item:
        return None
    day = Config.settle_day()
    waiting = conn.execute(
        "SELECT id, seq, rows_pending, created_day FROM candidates "
        "WHERE pipeline_id = ? AND status = 'waiting' AND rows_pending > 0 "
        "ORDER BY seq",
        (pipeline_id,),
    ).fetchall()
    if not waiting:
        raise BizError("候补池为空，没有可继续的候补")
    eligible = [c for c in waiting if c["created_day"] < day]
    if not eligible:
        raise BizError("候补池未过日期边界，次日才能继续")

    total = 0
    for c in eligible:
        loaded_today = _ledger_loaded(conn, pipeline_id, day)
        remaining = item["daily_quota"] - loaded_today
        if remaining <= 0:
            break
        grant = min(remaining, c["rows_pending"])
        _deduct(conn, pipeline_id, day, grant)
        left = c["rows_pending"] - grant
        conn.execute(
            "UPDATE candidates SET rows_pending = ?, status = ? WHERE id = ?",
            (left, "done" if left == 0 else "waiting", c["id"]),
        )
        _add_event(
            conn, pipeline_id, day, "候补续落", grant, remaining - grant,
            f"候补#{c['seq']} 按新日配额续落",
        )
        total += grant

    still = _pending_rows(conn, pipeline_id)
    new_status = item["status"]
    if still == 0 and item["status"] == "部分落库":
        new_status = "已落库"
    conn.execute(
        "UPDATE pipelines SET loaded = loaded + ?, status = ? WHERE id = ?",
        (total, new_status, pipeline_id),
    )
    return get_pipeline(conn, pipeline_id)
