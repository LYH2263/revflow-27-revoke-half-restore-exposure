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


def test_revoke_and_restore(client):
    run = client.post("/api/settle-runs").json()
    assert run["status"] == "完成"

    fwd = _entries(client, "正向")
    rev = _entries(client, "冲正")
    # REV-001 全额冲正 FWD-001；撤销后 FWD-001 应恢复全额敞口
    before_bal = _bal(client)
    assert fwd["FWD-20261116-001"]["remaining_cents"] == 0
    assert rev["REV-20261116-001"]["status"] == "已归并"

    r = client.post("/api/revocations", json={"reversal_key": "REV-20261116-001"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restored_cents"] == 10000
    assert body["remaining_cents"] == 10000
    assert body["balance_cents"] == before_bal[1] + 10000
    assert body["run_id"]

    # 状态与证据可查
    rev = _entries(client, "冲正")
    assert rev["REV-20261116-001"]["status"] == "已撤销"
    assert rev["REV-20261116-001"]["revoked_cents"] == 10000
    assert rev["REV-20261116-001"]["revoked_at"]
    assert rev["REV-20261116-001"]["revoked_run_id"]
    fwd = _entries(client, "正向")
    assert fwd["FWD-20261116-001"]["remaining_cents"] == 10000

    evid = client.get("/api/revocations").json()
    assert any(
        v["reversal_key"] == "REV-20261116-001" and v["restored_cents"] == 10000
        for v in evid
    )

    # 余额与流水净额互证仍一致：已撤销冲正不计入已归并净额，恢复额已回加余额
    report = client.post("/api/verify-reports").json()
    assert report["consistent"] == 1
    assert report["revoke_count"] == 1
    assert report["revoke_restored_cents"] == 10000
    assert report["detail"]["撤销笔数"] == 1
    assert report["detail"]["撤销恢复额分"] == 10000

    # 归并运行也单列撤销笔数与恢复额
    run_detail = client.get(f"/api/settle-runs/{run['id']}").json()
    assert run_detail["revoke_count"] == 1
    assert run_detail["revoke_restored_cents"] == 10000

    # 证据刷新后仍在
    assert client.get("/api/revocations").json()[0]["reversal_key"] == "REV-20261116-001"


def test_reject_non_settled_and_duplicate(client):
    # 已隔离冲正禁止走撤销成功路径
    bal_before = _bal(client)
    r = client.post("/api/revocations", json={"reversal_key": "REV-20261116-004"})
    assert r.status_code == 409
    rev = _entries(client, "冲正")
    assert rev["REV-20261116-004"]["status"] == "已隔离"
    assert _bal(client) == bal_before

    # 重复撤销被拒
    r = client.post("/api/revocations", json={"reversal_key": "REV-20261116-001"})
    assert r.status_code == 409

    # 不存在的键
    r = client.post("/api/revocations", json={"reversal_key": "NOPE"})
    assert r.status_code == 404

    count = len(client.get("/api/revocations").json())
    assert count == 1


def test_second_run_keeps_restored_balance(client):
    """归并后再归并（无新流水）不得抹掉撤销恢复的余额。"""
    bal_before = _bal(client)
    run = client.post("/api/settle-runs").json()
    assert run["status"] == "完成"
    assert _bal(client) == bal_before
    report = client.post("/api/verify-reports").json()
    assert report["consistent"] == 1
    assert report["revoke_count"] == 1
