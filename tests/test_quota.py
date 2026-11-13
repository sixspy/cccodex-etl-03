import threading

from fastapi.testclient import TestClient

from app.main import app

DAY1 = "2026-11-13"
DAY2 = "2026-11-14"


def _ids(authed):
    return [p["id"] for p in authed.get("/api/pipelines").json()["items"]]


def _set_quota(authed, pid, **kw):
    r = authed.post(f"/api/pipelines/{pid}/quota", json=kw)
    assert r.status_code == 200, r.text
    return r.json()


def _board_row(authed, pid):
    board = authed.get("/api/quota/board").json()
    return board, [i for i in board["items"] if i["id"] == pid][0]


def test_quota_deduction_and_board(authed, monkeypatch):
    """配额扣减在服务端记账；抽取条数可大于配额，当日落库数不得超过配额。"""
    monkeypatch.setenv("ETL_SETTLE_DAY", DAY1)
    pid = _ids(authed)[0]
    _set_quota(authed, pid, daily_quota=5, overflow_path="pool")

    ex = authed.post(f"/api/pipelines/{pid}/extract")
    assert ex.status_code == 200
    extracted = ex.json()["extracted"]
    assert extracted > 5  # 抽取条数可以大于配额

    ld = authed.post(f"/api/pipelines/{pid}/load")
    assert ld.status_code == 200
    data = ld.json()
    assert data["status"] == "部分落库"
    assert data["loaded"] == 5
    assert data["today_loaded"] == 5  # 当日成功落库数不得超过配额
    assert data["remaining"] == 0
    assert data["candidates_pending_rows"] == extracted - 5

    board, row = _board_row(authed, pid)
    assert board["settle_day"] == DAY1
    assert row["daily_quota"] == 5
    assert row["today_loaded"] == 5
    assert row["candidates_pending_rows"] == extracted - 5
    assert row["overflow_path_name"]
    kinds = [e["kind"] for e in data["events"]]
    assert "扣减" in kinds and "候补入账" in kinds


def test_fail_path_quota_full(authed, monkeypatch):
    """超额当场失败并写配额已满，不扣减不入候补。"""
    monkeypatch.setenv("ETL_SETTLE_DAY", DAY1)
    pid = _ids(authed)[1]
    _set_quota(authed, pid, daily_quota=3, overflow_path="fail")
    authed.post(f"/api/pipelines/{pid}/extract")

    r = authed.post(f"/api/pipelines/{pid}/load")
    assert r.status_code == 400
    assert r.json()["detail"] == "配额已满"

    detail = authed.get(f"/api/pipelines/{pid}").json()
    assert detail["today_loaded"] == 0
    assert detail["candidates_pending_rows"] == 0
    assert detail["status"] == "已抽取待加载"
    assert any(e["kind"] == "配额已满" for e in detail["events"])


def test_skewed_overflow_path_blocked(authed, monkeypatch):
    """落库时超额路径偏离说明，返回超额路径写歪了。"""
    monkeypatch.setenv("ETL_SETTLE_DAY", DAY1)
    pid = _ids(authed)[2]
    _set_quota(authed, pid, daily_quota=2, overflow_path="pool")
    authed.post(f"/api/pipelines/{pid}/extract")

    r = authed.post(f"/api/pipelines/{pid}/load", json={"overflow_path": "fail"})
    assert r.status_code == 400
    assert r.json()["detail"] == "超额路径写歪了"

    detail = authed.get(f"/api/pipelines/{pid}").json()
    assert detail["today_loaded"] == 0
    assert detail["candidates_pending_rows"] == 0


def test_path_locked_same_day(authed, monkeypatch):
    """超额路径只能选一种写进说明，当日禁止改口，次日可改。"""
    monkeypatch.setenv("ETL_SETTLE_DAY", DAY1)
    pid = _ids(authed)[0]
    _set_quota(authed, pid, overflow_path="pool")

    r = authed.post(f"/api/pipelines/{pid}/quota", json={"overflow_path": "fail"})
    assert r.status_code == 400
    assert "当日禁止改口" in r.json()["detail"]

    again = authed.post(f"/api/pipelines/{pid}/quota", json={"overflow_path": "pool"})
    assert again.status_code == 200  # 同值不算改口

    monkeypatch.setenv("ETL_SETTLE_DAY", DAY2)
    r2 = authed.post(f"/api/pipelines/{pid}/quota", json={"overflow_path": "fail"})
    assert r2.status_code == 200
    assert r2.json()["overflow_path"] == "fail"


def test_load_before_extract_blocked(authed):
    """管道还没抽完不能落库。"""
    pid = _ids(authed)[0]
    r = authed.post(f"/api/pipelines/{pid}/load")
    assert r.status_code == 400
    assert "管道还没抽完" in r.json()["detail"]


def test_candidates_continue_next_day(authed, monkeypatch):
    """候补与隔日继续：隔日不自动落，点继续按新日配额重算。"""
    monkeypatch.setenv("ETL_SETTLE_DAY", DAY1)
    pid = _ids(authed)[3]
    _set_quota(authed, pid, daily_quota=5, overflow_path="pool")
    extracted = authed.post(f"/api/pipelines/{pid}/extract").json()["extracted"]
    overflow = extracted - 5
    authed.post(f"/api/pipelines/{pid}/load")

    # 当日点继续：未过日期边界
    r = authed.post(f"/api/pipelines/{pid}/candidates/continue")
    assert r.status_code == 400
    assert "次日" in r.json()["detail"]

    # 跨日后不自动落
    monkeypatch.setenv("ETL_SETTLE_DAY", DAY2)
    detail = authed.get(f"/api/pipelines/{pid}").json()
    assert detail["today_loaded"] == 0
    assert detail["candidates_pending_rows"] == overflow
    board, row = _board_row(authed, pid)
    assert row["today_loaded"] == 0
    assert row["candidates_pending_rows"] == overflow

    # 点继续，按新日配额重算
    r2 = authed.post(f"/api/pipelines/{pid}/candidates/continue")
    assert r2.status_code == 200
    data = r2.json()
    assert data["today_loaded"] == 5
    assert data["candidates_pending_rows"] == overflow - 5
    seqs = [c["seq"] for c in data["candidates"]]
    assert seqs == sorted(seqs)  # 候补顺序可见
    assert any(e["kind"] == "候补续落" for e in data["events"])

    # 新日配额已用尽，再点不多落
    r3 = authed.post(f"/api/pipelines/{pid}/candidates/continue")
    assert r3.status_code == 200
    assert r3.json()["today_loaded"] == 5
    assert r3.json()["candidates_pending_rows"] == overflow - 5

    # 再跨一日，续完全部
    monkeypatch.setenv("ETL_SETTLE_DAY", "2026-11-15")
    r4 = authed.post(f"/api/pipelines/{pid}/candidates/continue")
    assert r4.status_code == 200
    left = overflow - 5
    assert r4.json()["today_loaded"] == min(5, left)
    if left <= 5:
        assert r4.json()["candidates_pending_rows"] == 0
        assert r4.json()["status"] == "已落库"


def test_last_slot_single_winner(authed, monkeypatch):
    """两人同时争抢最后一个名额只成一笔。"""
    monkeypatch.setenv("ETL_SETTLE_DAY", DAY1)
    pid = _ids(authed)[0]
    extracted = authed.post(f"/api/pipelines/{pid}/extract").json()["extracted"]
    _set_quota(authed, pid, daily_quota=extracted, overflow_path="fail")

    clients = []
    for _ in range(2):
        c = TestClient(app)
        r = c.post("/api/login", json={"username": "admin", "password": "123456"})
        assert r.status_code == 200
        clients.append(c)

    barrier = threading.Barrier(2)
    results = []

    def hit(client):
        barrier.wait()
        results.append(client.post(f"/api/pipelines/{pid}/load"))

    threads = [threading.Thread(target=hit, args=(c,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [r for r in results if r.status_code == 200]
    assert len(oks) == 1  # 只成一笔
    assert oks[0].json()["today_loaded"] == extracted

    detail = authed.get(f"/api/pipelines/{pid}").json()
    assert detail["today_loaded"] == extracted  # 没有超扣
    assert detail["remaining"] == 0


def test_pages_expose_quota_and_candidates(authed, monkeypatch):
    """页面能读出配额与候补；总览行带配额、今日已落、候补条数、路径名称。"""
    monkeypatch.setenv("ETL_SETTLE_DAY", DAY1)
    page = authed.get("/app/")
    assert page.status_code == 200
    for kw in ("配额看板", "管道总览", "落库详情", "超额设置", "候补"):
        assert kw in page.text

    items = authed.get("/api/pipelines").json()["items"]
    row = items[0]
    for key in (
        "daily_quota",
        "today_loaded",
        "remaining",
        "candidates_pending_rows",
        "overflow_path_name",
    ):
        assert key in row

    board = authed.get("/api/quota/board").json()
    assert board["settle_day"] == DAY1
    assert board["totals"]["quota"] == sum(i["daily_quota"] for i in items)
