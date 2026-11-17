from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dashboard() -> dict:
    with connect() as conn:
        accounts = rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())
        fwd = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '正向'"
        ).fetchone()["c"]
        rev = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '冲正'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE status = '待归并'"
        ).fetchone()["c"]
        revoked = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '冲正' AND status = '已撤销'"
        ).fetchone()["c"]
        restored = conn.execute(
            "SELECT COALESCE(SUM(restored_cents), 0) AS s FROM reverse_revocations"
        ).fetchone()["s"]
        q = conn.execute("SELECT COUNT(*) AS c FROM quarantine_items").fetchone()["c"]
        bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
        last = conn.execute("SELECT * FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "账户数": len(accounts),
            "正向笔数": fwd,
            "冲正笔数": rev,
            "待归并": pending,
            "已撤销": revoked,
            "撤销恢复额分": restored,
            "未平隔离": q,
            "余额合计分": bal,
            "最近归并": dict(last) if last else None,
            "账户": accounts,
        }


def list_accounts() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())


def list_entries(kind: str | None = None) -> list[dict]:
    with connect() as conn:
        if kind:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                WHERE e.kind = ?
                ORDER BY e.id
                """,
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                ORDER BY e.id
                """
            ).fetchall()
        out = rows_to_dicts(rows)
        # 正向：附带剩余敞口（已撤销冲正不计入已冲累计）
        used_by_target = {
            r["target_entry_key"]: r["s"]
            for r in conn.execute(
                """
                SELECT target_entry_key, COALESCE(SUM(amount_cents), 0) AS s
                FROM ledger_entries
                WHERE kind = '冲正' AND status = '已归并' AND target_entry_key IS NOT NULL
                GROUP BY target_entry_key
                """
            ).fetchall()
        }
        for e in out:
            if e["kind"] == "正向":
                e["remaining_cents"] = e["amount_cents"] - used_by_target.get(e["entry_key"], 0)
        return out


def list_quarantine() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT q.*, a.code AS account_code
            FROM quarantine_items q
            JOIN accounts a ON a.id = q.account_id
            ORDER BY q.id DESC
            """
        ).fetchall()
        return rows_to_dicts(rows)


def list_settle_runs() -> list[dict]:
    with connect() as conn:
        rows = rows_to_dicts(
            conn.execute("SELECT * FROM settle_runs ORDER BY id DESC").fetchall()
        )
        for r in rows:
            _attach_revoke_rollup(conn, r)
        return rows


def _attach_revoke_rollup(conn, run: dict) -> None:
    """归并报告单列撤销笔数与恢复额（与撤销记录、冲正状态同源）。"""
    row = conn.execute(
        """
        SELECT COUNT(*) AS c, COALESCE(SUM(v.restored_cents), 0) AS s
        FROM reverse_revocations v
        JOIN ledger_entries e ON e.entry_key = v.reversal_key
        WHERE e.settle_run_id = ?
        """,
        (run["id"],),
    ).fetchone()
    run["revoke_count"] = row["c"]
    run["revoke_restored_cents"] = row["s"]


def list_verify_reports() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM verify_reports ORDER BY id DESC").fetchall()
        )


def run_settle() -> dict:
    """按账户汇总正向−已匹配冲正；无原单/超额冲正进隔离；增量累加账户余额。

    余额采用增量记账：本轮净额只叠加到既有余额上。撤销冲正时已把恢复额
    回加余额，故后续归并不能用「本轮净额覆盖写」抹掉撤销的恢复。
    """
    with connect() as conn:
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO settle_runs(status, started_at)
            VALUES ('进行中', ?)
            """,
            (now,),
        )
        run_id = cur.lastrowid

        forwards = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE kind = '正向' AND status = '待归并' ORDER BY id"
            ).fetchall()
        )
        reversals = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE kind = '冲正' AND status = '待归并' ORDER BY id"
            ).fetchall()
        )
        # 冲正可挂到本轮待归并正向，也可挂到历史已归并正向（撤销恢复的敞口可再被冲）
        all_forwards = {
            f["entry_key"]: f
            for f in rows_to_dicts(
                conn.execute("SELECT * FROM ledger_entries WHERE kind = '正向'").fetchall()
            )
        }

        # 每个正向已占用冲正额度：含历史已归并冲正（不含已撤销）
        used: dict[str, int] = {}
        for r in conn.execute(
            """
            SELECT target_entry_key, COALESCE(SUM(amount_cents), 0) AS s
            FROM ledger_entries
            WHERE kind = '冲正' AND status = '已归并' AND target_entry_key IS NOT NULL
            GROUP BY target_entry_key
            """
        ).fetchall():
            used[r["target_entry_key"]] = r["s"]
        for f in forwards:
            used.setdefault(f["entry_key"], 0)
        settled = 0
        quarantined = 0
        account_net: dict[int, int] = {}

        for f in forwards:
            account_net[f["account_id"]] = account_net.get(f["account_id"], 0) + f["amount_cents"]

        for r in reversals:
            target = r.get("target_entry_key")
            if not target or target not in all_forwards:
                conn.execute(
                    """
                    INSERT INTO quarantine_items(
                      entry_key, account_id, reason, amount_cents, settle_run_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (r["entry_key"], r["account_id"], "冲正无原单", r["amount_cents"], run_id, now),
                )
                conn.execute(
                    "UPDATE ledger_entries SET status = '已隔离', settle_run_id = ? WHERE id = ?",
                    (run_id, r["id"]),
                )
                quarantined += 1
                continue

            fwd = all_forwards[target]
            if fwd["account_id"] != r["account_id"]:
                conn.execute(
                    """
                    INSERT INTO quarantine_items(
                      entry_key, account_id, reason, amount_cents, settle_run_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (r["entry_key"], r["account_id"], "冲正账户与原单不一致", r["amount_cents"], run_id, now),
                )
                conn.execute(
                    "UPDATE ledger_entries SET status = '已隔离', settle_run_id = ? WHERE id = ?",
                    (run_id, r["id"]),
                )
                quarantined += 1
                continue

            remain = fwd["amount_cents"] - used.get(target, 0)
            if r["amount_cents"] > remain:
                conn.execute(
                    """
                    INSERT INTO quarantine_items(
                      entry_key, account_id, reason, amount_cents, settle_run_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (r["entry_key"], r["account_id"], "冲正超额", r["amount_cents"], run_id, now),
                )
                conn.execute(
                    "UPDATE ledger_entries SET status = '已隔离', settle_run_id = ? WHERE id = ?",
                    (run_id, r["id"]),
                )
                quarantined += 1
                continue

            used[target] += r["amount_cents"]
            account_net[r["account_id"]] = account_net.get(r["account_id"], 0) - r["amount_cents"]
            conn.execute(
                "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                (run_id, r["id"]),
            )
            settled += 1

        for f in forwards:
            conn.execute(
                "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                (run_id, f["id"]),
            )
            settled += 1

        net_total = 0
        for acc_id, net in account_net.items():
            conn.execute(
                "UPDATE accounts SET balance_cents = balance_cents + ?, last_settle_run_id = ? WHERE id = ?",
                (net, run_id, acc_id),
            )
            net_total += net

        # 无流水账户余额清零（本轮未触及则保持；底座简化：仅写触及账户）
        finished = _now()
        conn.execute(
            """
            UPDATE settle_runs
            SET status = '完成',
                forward_count = ?,
                reversal_count = ?,
                settled_count = ?,
                quarantine_count = ?,
                net_cents = ?,
                finished_at = ?
            WHERE id = ?
            """,
            (
                len(forwards),
                len(reversals),
                settled,
                quarantined,
                net_total,
                finished,
                run_id,
            ),
        )
        conn.commit()
        return get_settle_run(run_id)


def get_settle_run(run_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "归并运行不存在")
        out = dict(row)
        _attach_revoke_rollup(conn, out)
        return out


def run_verify(settle_run_id: int | None = None) -> dict:
    with connect() as conn:
        if settle_run_id is None:
            last = conn.execute("SELECT id FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
            if not last:
                raise HTTPException(400, "尚无归并运行，无法互证")
            settle_run_id = last["id"]
        run = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (settle_run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "归并运行不存在")

        # 已归并正向 − 已归并冲正
        fwd_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
            WHERE kind = '正向' AND status = '已归并' AND settle_run_id = ?
            """,
            (settle_run_id,),
        ).fetchone()["s"]
        rev_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
            WHERE kind = '冲正' AND status = '已归并' AND settle_run_id = ?
            """,
            (settle_run_id,),
        ).fetchone()["s"]
        ledger_net = fwd_sum - rev_sum
        balance_sum = conn.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts WHERE last_settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["s"]
        # 账户余额为增量累加；被撤销冲正的恢复额已即时回加余额，
        # 因此「全量已归并流水净额」与「全量账户余额」始终应对齐。
        all_bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
        q_count = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_items WHERE settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["c"]

        # 全量已归并净额
        all_fwd = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '正向' AND status = '已归并'"
        ).fetchone()["s"]
        all_rev = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '冲正' AND status = '已归并'"
        ).fetchone()["s"]
        all_ledger_net = all_fwd - all_rev

        # 撤销单一来源：撤销笔数、恢复额、及被撤销冲正挂靠的入账轮
        revoke_rows = conn.execute(
            """
            SELECT v.*, e.settle_run_id AS reversal_settle_run_id
            FROM reverse_revocations v
            LEFT JOIN ledger_entries e ON e.entry_key = v.reversal_key
            ORDER BY v.id
            """
        ).fetchall()
        revoked_count = len(revoke_rows)
        revoked_restored = sum(r["restored_cents"] for r in revoke_rows)
        run_revokes = [r for r in revoke_rows if r["reversal_settle_run_id"] == settle_run_id]

        consistent = 1 if all_ledger_net == all_bal else 0
        detail = {
            "本轮正向分": fwd_sum,
            "本轮冲正分": rev_sum,
            "本轮流水净额分": ledger_net,
            "本轮触及余额合计分": balance_sum,
            "全量流水净额分": all_ledger_net,
            "全量账户余额分": all_bal,
            "本轮隔离数": q_count,
            "撤销笔数": revoked_count,
            "本轮撤销笔数": len(run_revokes),
            "撤销恢复额分": revoked_restored,
            "本轮撤销恢复额分": sum(r["restored_cents"] for r in run_revokes),
            "非归并状态冲正分": conn.execute(
                """
                SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
                WHERE kind = '冲正' AND status NOT IN ('已归并', '已撤销')
                """
            ).fetchone()["s"],
        }
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO verify_reports(
              settle_run_id, ledger_net_cents, balance_sum_cents,
              quarantine_count, consistent, detail_json,
              revoke_count, revoke_restored_cents, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settle_run_id,
                all_ledger_net,
                all_bal,
                q_count,
                consistent,
                json.dumps(detail, ensure_ascii=False),
                revoked_count,
                revoked_restored,
                now,
            ),
        )
        conn.commit()
        rid = cur.lastrowid
        row = conn.execute("SELECT * FROM verify_reports WHERE id = ?", (rid,)).fetchone()
        out = dict(row)
        out["detail"] = detail
        return out
