"""已归并冲正整笔撤销：敞口恢复 + 余额回滚 + 撤销证据，同一事务落定。

## 落库形态（择一，本仓采用）
**独立撤销事件表** ``reverse_revocations`` 为撤销事实的唯一记账来源；
冲正流水上的 ``status='已撤销'`` 与 ``revoked_at/revoked_run_id/revoked_cents``
只是同一事务内派生的行级状态快照（供流水页展示），不参与恢复额计算。
恢复额、敞口与余额一律以撤销事件 + 恢复证明表为准。

## 恢复路径（择一，本仓采用）
**同事务完成「冲正改已撤销 + 回加敞口 + 回加余额 + 写事件/证明」，缺一则整笔回滚。**
不采用「先写事件草稿再切换」的两段式，故不存在「事件已可见而敞口/余额未变」的窗口。

恢复证明实体 ``revoke_restore_proofs``（基线所无）记录冲正键、原单敞口前后、
账户余额前后，供 ``GET /api/revocations/{id}/proof`` 只读查询。
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException

from app.db import connect, rows_to_dicts
from app.services.settle import _now


def _revocation_row(row) -> dict:
    return dict(row)


def _proof_row(row) -> dict:
    return dict(row)


_PROOF_FIELDS = """
    p.id AS proof_id,
    p.exposure_before_cents,
    p.exposure_after_cents,
    p.balance_before_cents,
    p.balance_after_cents,
    p.created_at AS proof_created_at
"""


def list_revocations() -> list[dict]:
    """撤销事件清单（含恢复证明字段，同源派生）。"""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT v.*, a.code AS account_code, a.name AS account_name,
                   {_PROOF_FIELDS}
            FROM reverse_revocations v
            JOIN accounts a ON a.id = v.account_id
            LEFT JOIN revoke_restore_proofs p ON p.revocation_id = v.id
            ORDER BY v.id DESC
            """
        ).fetchall()
        return rows_to_dicts(rows)


def get_revocation_proof(revocation_id: int) -> dict:
    """按撤销事件 id 只读查询恢复证明。"""
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT v.id AS revocation_id, v.reversal_key, v.target_entry_key,
                   v.account_id, v.restored_cents, v.run_id, v.revoked_at,
                   {_PROOF_FIELDS}
            FROM reverse_revocations v
            LEFT JOIN revoke_restore_proofs p ON p.revocation_id = v.id
            WHERE v.id = ?
            """,
            (revocation_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "撤销事件不存在")
        out = _proof_row(row)
        if out.get("proof_id") is None:
            # 理论上不会发生：事件与证明同事务写入
            raise HTTPException(409, "撤销事件缺少恢复证明，数据不一致")
        # 兼容撤销事件主键命名（id 与 revocation_id 同值）
        out["id"] = out["revocation_id"]
        return out


def revoke_summary(conn) -> dict:
    """撤销笔数与恢复额（与撤销记录同源）。"""
    row = conn.execute(
        """
        SELECT COUNT(*) AS c, COALESCE(SUM(restored_cents), 0) AS s
        FROM reverse_revocations
        """
    ).fetchone()
    return {"撤销笔数": row["c"], "恢复额分": row["s"]}


def remaining_exposure(conn, forward_row) -> int:
    """正向剩余敞口 = 正向额 − 仍计入已冲累计的冲正额。

    仅「已归并」冲正占用敞口；「已撤销」冲正不再占用（撤销时已回加敞口），
    「已隔离」冲正本就未计入。与 ``settle.list_entries`` 同一口径。
    """
    used = conn.execute(
        """
        SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
        WHERE kind = '冲正' AND status = '已归并' AND target_entry_key = ?
        """,
        (forward_row["entry_key"],),
    ).fetchone()["s"]
    return forward_row["amount_cents"] - used


def _write_proof(conn, *, revocation_id, run_id, rev, restored, exposure_before,
                 exposure_after, balance_before, balance_after, now) -> int:
    cur = conn.execute(
        """
        INSERT INTO revoke_restore_proofs(
          revocation_id, run_id, reversal_key, target_entry_key, account_id,
          restored_cents, exposure_before_cents, exposure_after_cents,
          balance_before_cents, balance_after_cents, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revocation_id,
            run_id,
            rev["entry_key"],
            rev["target_entry_key"],
            rev["account_id"],
            restored,
            exposure_before,
            exposure_after,
            balance_before,
            balance_after,
            now,
        ),
    )
    return cur.lastrowid


def revoke_reversal(reversal_key: str, run_id: str | None = None) -> dict:
    """对「已归并」冲正发起整笔撤销：状态、敞口、余额、事件与证明同事务落定。"""
    run_id = (run_id or f"RVK-{uuid.uuid4().hex[:12]}").strip()
    if not reversal_key or not reversal_key.strip():
        raise HTTPException(400, "缺少冲正流水键")

    conn = connect()
    conn.isolation_level = None  # 显式事务：状态/敞口/余额/事件/证明同一失败闭
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

        # 撤销前快照：敞口（仅已归并冲正占用）与账户余额
        exposure_before = remaining_exposure(conn, fwd)
        balance_before = account["balance_cents"]

        now = _now()
        # 1) 冲正状态机进入「已撤销」并写行级快照（同事务派生，非记账来源）
        cur = conn.execute(
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
        if cur.rowcount != 1:
            raise HTTPException(409, "冲正状态已变化，撤销中止")

        # 2) 回加账户余额（此前缺失的一步：只恢复敞口不回加余额＝半恢复）
        cur = conn.execute(
            "UPDATE accounts SET balance_cents = balance_cents + ? WHERE id = ?",
            (restored, rev["account_id"]),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "账户不存在，撤销中止")

        # 3) 撤销事件（唯一记账来源）
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
        revocation_id = cur.lastrowid

        # 4) 恢复证明（冲正键 + 敞口前后 + 余额前后），与事件同事务
        exposure_after = exposure_before + restored
        balance_after = balance_before + restored
        _write_proof(
            conn,
            revocation_id=revocation_id,
            run_id=run_id,
            rev=rev,
            restored=restored,
            exposure_before=exposure_before,
            exposure_after=exposure_after,
            balance_before=balance_before,
            balance_after=balance_after,
            now=now,
        )

        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 — 整笔失败闭
        conn.rollback()
        raise HTTPException(500, f"撤销失败，已整笔回滚：{exc}") from exc
    finally:
        conn.close()

    out = get_revocation_proof(revocation_id)
    out["balance_cents"] = out["balance_after_cents"]
    out["remaining_cents"] = out["exposure_after_cents"]
    out["reversal_status"] = "已撤销"
    return out
