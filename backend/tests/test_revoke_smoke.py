"""撤销半恢复 / 事件不落库回归。

覆盖硬约束：
- 撤销必须恢复敞口并回滚余额，失败整笔回到撤销前；
- 事件、敞口、余额同事务，禁止「事件已可见而敞口未恢复」，
  禁止「余额已回加但冲正仍计已归并」；
- 恢复证明实体可按撤销事件 id 只读查询，冲正页与互证同源派生。
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _entries(client, kind):
    return {e["entry_key"]: e for e in client.get(f"/api/entries?kind={kind}").json()}


def _bal(client):
    return {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}


def _db(client):
    from app import db as dbmod

    conn = sqlite3.connect(dbmod.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def test_revoke_restores_exposure_and_balance(client):
    run = client.post("/api/settle-runs").json()
    assert run["status"] == "完成"

    before_bal = _bal(client)
    before_fwd = _entries(client, "正向")
    assert before_fwd["FWD-20261116-001"]["remaining_cents"] == 0

    r = client.post("/api/revocations", json={"reversal_key": "REV-20261116-001"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restored_cents"] == 10000
    assert body["exposure_before_cents"] == 0
    assert body["exposure_after_cents"] == 10000
    assert body["balance_before_cents"] == before_bal[1]
    assert body["balance_after_cents"] == before_bal[1] + 10000
    assert body["balance_cents"] == before_bal[1] + 10000
    assert body["remaining_cents"] == 10000

    # 状态、敞口与余额三处同时生效
    assert _entries(client, "冲正")["REV-20261116-001"]["status"] == "已撤销"
    assert _entries(client, "正向")["FWD-20261116-001"]["remaining_cents"] == 10000
    assert _bal(client)[1] == before_bal[1] + 10000

    # 撤销事件 + 恢复证明可查（按撤销事件 id 只读）
    proof = client.get(f"/api/revocations/{body['id']}/proof")
    assert proof.status_code == 200, proof.text
    pj = proof.json()
    assert pj["reversal_key"] == "REV-20261116-001"
    assert pj["target_entry_key"] == "FWD-20261116-001"
    assert pj["exposure_before_cents"] == 0
    assert pj["exposure_after_cents"] == 10000
    assert pj["balance_before_cents"] == before_bal[1]
    assert pj["balance_after_cents"] == before_bal[1] + 10000

    # 冲正流水上的证明字段与证明接口同源
    rev = _entries(client, "冲正")["REV-20261116-001"]
    assert rev["restore_proof"]["exposure_after_cents"] == pj["exposure_after_cents"]
    assert rev["restore_proof"]["balance_after_cents"] == pj["balance_after_cents"]

    # 流水净额与余额互证一致（已撤销冲正剔除、恢复额回加余额）
    report = client.post("/api/verify-reports").json()
    assert report["consistent"] == 1
    assert report["revoke_count"] == 1
    assert report["revoke_restored_cents"] == 10000


def test_reject_non_settled_and_duplicate(client):
    bal_before = _bal(client)
    n_events = len(client.get("/api/revocations").json())

    r = client.post("/api/revocations", json={"reversal_key": "REV-20261116-004"})
    assert r.status_code == 409
    assert _entries(client, "冲正")["REV-20261116-004"]["status"] == "已隔离"
    assert _bal(client) == bal_before

    r = client.post("/api/revocations", json={"reversal_key": "REV-20261116-001"})
    assert r.status_code == 409
    r = client.post("/api/revocations", json={"reversal_key": "NOPE"})
    assert r.status_code == 404

    assert len(client.get("/api/revocations").json()) == n_events
    assert _bal(client) == bal_before

    # 无对应事件的证明查询 404
    assert client.get("/api/revocations/99999/proof").status_code == 404


def test_failed_revoke_rolls_back_everything(client):
    """事件写入失败时，冲正状态、敞口、余额必须全部留在撤销前。"""
    bal_before = _bal(client)
    fwd_before = _entries(client, "正向")["FWD-20261116-002"]["remaining_cents"]
    rev_before = _entries(client, "冲正")["REV-20261116-002"]
    n_events = len(client.get("/api/revocations").json())
    conn = _db(client)
    try:
        n_proofs = conn.execute("SELECT COUNT(*) AS c FROM revoke_restore_proofs").fetchone()["c"]
        # 让证明写入失败：在同一事务内制造约束冲突（证明表唯一外键）
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS fail_proof_insert
            BEFORE INSERT ON revoke_restore_proofs
            BEGIN
              SELECT RAISE(ABORT, 'injected proof failure');
            END
            """
        )
        conn.commit()
    finally:
        conn.close()

    try:
        r = client.post("/api/revocations", json={"reversal_key": "REV-20261116-002"})
        assert r.status_code == 500, r.text
    finally:
        conn = _db(client)
        try:
            conn.execute("DROP TRIGGER IF EXISTS fail_proof_insert")
            conn.commit()
        finally:
            conn.close()

    # 整笔回到撤销前：状态未变、敞口未变、余额未变、无事件、无证明
    assert _entries(client, "冲正")["REV-20261116-002"]["status"] == rev_before["status"]
    assert _entries(client, "正向")["FWD-20261116-002"]["remaining_cents"] == fwd_before
    assert _bal(client) == bal_before
    assert len(client.get("/api/revocations").json()) == n_events
    conn = _db(client)
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM revoke_restore_proofs").fetchone()["c"] == n_proofs
    finally:
        conn.close()

    # 约束解除后重试成功，且恢复额、余额、敞口同一次落定
    r = client.post("/api/revocations", json={"reversal_key": "REV-20261116-002"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restored_cents"] == 5000
    assert body["balance_after_cents"] == body["balance_before_cents"] + 5000
    assert body["exposure_after_cents"] == body["exposure_before_cents"] + 5000
    assert _bal(client)[1] == bal_before[1] + 5000


def test_second_run_keeps_restored_balance(client):
    bal_before = _bal(client)
    run = client.post("/api/settle-runs").json()
    assert run["status"] == "完成"
    assert _bal(client) == bal_before
    report = client.post("/api/verify-reports").json()
    assert report["consistent"] == 1
    assert report["revoke_count"] == 2
    assert report["revoke_restored_cents"] == 15000
