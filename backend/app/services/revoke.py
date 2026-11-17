"""已归并冲正整笔撤销与敞口恢复。

落库形态：独立撤销事件表 ``reverse_revocations``（不可改不可删），
并在冲正流水上写入「已撤销」状态与不可变撤销快照列
（``revoked_at`` / ``revoked_run_id`` / ``revoked_cents``）。
撤销、敞口恢复与余额回加在同一事务内提交；任一失败整笔回滚。
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException

from app.db import connect, rows_to_dicts
from app.services.settle import _now


def _revocation_row(row) -> dict:
    return dict(row)


def list_revocations() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT v.*, a.code AS account_code, a.name AS account_name
            FROM reverse_revocations v
            JOIN accounts a ON a.id = v.account_id
            ORDER BY v.id DESC
            """
        ).fetchall()
        return rows_to_dicts(rows)


def revoke_summary(conn) -> dict:
    """撤销笔数与恢复额（与撤销记录同源）。"""
    row = conn.execute(
        """
        SELECT COUNT(*) AS c, COALESCE(SUM(restored_cents), 0) AS s
        FROM reverse_revocations
        """
    ).fetchone()
    return {"撤销笔数": row["c"], "恢复额分": row["s"]}


def _remaining_exposure(conn, forward_row) -> int:
    """正向剩余敞口 = 正向额 − 仍计入已冲累计的冲正额。

    已撤销的冲正不再计入已冲累计，撤销额度自然回到敞口。
    """
    used = conn.execute(
        """
        SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
        WHERE kind = '冲正' AND status IN ('已归并', '已撤销') AND target_entry_key = ?
        """,
        (forward_row["entry_key"],),
    ).fetchone()["s"]
    return forward_row["amount_cents"] - used


def revoke_reversal(reversal_key: str, run_id: str | None = None) -> dict:
    """对「已归并」冲正发起整笔撤销：状态、撤销证据、敞口、余额同事务落定。"""
    run_id = (run_id or f"RVK-{uuid.uuid4().hex[:12]}").strip()
    if not reversal_key or not reversal_key.strip():
        raise HTTPException(400, "缺少冲正流水键")

    conn = connect()
    conn.isolation_level = None  # 显式事务：撤销、证据、敞口、余额同一失败闭
    try:
        conn.execute("BEGIN IMMEDIATE")
        rev = conn.execute(
            "SELECT * FROM ledger_entries WHERE entry_key = ?", (reversal_key.strip(),)
        ).fetchone()
        if not rev:
            raise HTTPException(404, "冲正流水不存在")
        if rev["kind"] != "冲正":
            raise HTTPException(400, "仅冲正流水可撤销")

        # 幂等/重复撤销先行判断，避免把已撤销当作非法状态
        if rev["revoked_at"] or rev["status"] == "已撤销":
            raise HTTPException(409, "该冲正已撤销，不得重复撤销")
        if rev["status"] != "已归并":
            raise HTTPException(
                409, f"仅「已归并」冲正可整笔撤销；当前状态为「{rev['status']}」"
            )
        if not rev["target_entry_key"]:
            raise HTTPException(409, "冲正未挂原单，不得撤销")

        fwd = conn.execute(
            "SELECT * FROM ledger_entries WHERE entry_key = ? AND kind = '正向'",
            (rev["target_entry_key"],),
        ).fetchone()
        if not fwd:
            raise HTTPException(409, "原正向流水缺失，不得撤销")
        if fwd["account_id"] != rev["account_id"]:
            raise HTTPException(409, "冲正账户与原单不一致，不得撤销")

        account = conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (rev["account_id"],)
        ).fetchone()
        if not account:
            raise HTTPException(409, "账户缺失，不得撤销")

        restored = rev["amount_cents"]
        if restored <= 0:
            raise HTTPException(409, "撤销额度非法")

        # 敞口守门：该原单累计恢复额不得超过原单金额
        already = conn.execute(
            """
            SELECT COALESCE(SUM(restored_cents), 0) AS s FROM reverse_revocations
            WHERE target_entry_key = ?
            """,
            (fwd["entry_key"],),
        ).fetchone()["s"]
        if already + restored > fwd["amount_cents"]:
            raise HTTPException(409, "撤销恢复额将超出原正向敞口，拒绝撤销")

        now = _now()
        # 冲正状态机进入「已撤销」并写不可变快照列
        conn.execute(
            """
            UPDATE ledger_entries
            SET status = '已撤销',
                revoked_at = ?,
                revoked_run_id = ?,
                revoked_cents = ?
            WHERE id = ? AND status = '已归并'
            """,
            (now, run_id, restored, rev["id"]),
        )
        # 撤销事件（不可改不可删）
        cur = conn.execute(
            """
            INSERT INTO reverse_revocations(
              reversal_key, reversal_id, target_entry_key, account_id,
              restored_cents, settle_run_id, run_id, revoked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rev["entry_key"],
                rev["id"],
                rev["target_entry_key"],
                rev["account_id"],
                restored,
                rev["settle_run_id"],
                run_id,
                now,
            ),
        )
        conn.commit()

        evidence = conn.execute(
            "SELECT * FROM reverse_revocations WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 — 整笔失败闭
        conn.rollback()
        raise HTTPException(500, f"撤销失败，已整笔回滚：{exc}") from exc
    finally:
        conn.close()

    with connect() as after:
        fwd_after = after.execute(
            "SELECT * FROM ledger_entries WHERE entry_key = ? AND kind = '正向'",
            (rev["target_entry_key"],),
        ).fetchone()
        bal = after.execute(
            "SELECT balance_cents FROM accounts WHERE id = ?", (rev["account_id"],)
        ).fetchone()["balance_cents"]
        remaining = _remaining_exposure(after, fwd_after)
        out = _revocation_row(evidence)
        out["balance_cents"] = bal
        out["remaining_cents"] = remaining + int(evidence["restored_cents"] or 0)
        out["reversal_status"] = "已撤销"
        return out
