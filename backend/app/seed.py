from __future__ import annotations

import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CNY',
  balance_cents INTEGER NOT NULL DEFAULT 0,
  last_settle_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS ledger_entries (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL UNIQUE,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  kind TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  biz_ref TEXT NOT NULL DEFAULT '',
  target_entry_key TEXT,
  booked_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '待归并',
  note TEXT NOT NULL DEFAULT '',
  settle_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS quarantine_items (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL,
  account_id INTEGER NOT NULL,
  reason TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  settle_run_id INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settle_runs (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  forward_count INTEGER NOT NULL DEFAULT 0,
  reversal_count INTEGER NOT NULL DEFAULT 0,
  settled_count INTEGER NOT NULL DEFAULT 0,
  quarantine_count INTEGER NOT NULL DEFAULT 0,
  net_cents INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS verify_reports (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER REFERENCES settle_runs(id),
  ledger_net_cents INTEGER NOT NULL,
  balance_sum_cents INTEGER NOT NULL,
  quarantine_count INTEGER NOT NULL,
  consistent INTEGER NOT NULL,
  detail_json TEXT NOT NULL,
  revoke_count INTEGER NOT NULL DEFAULT 0,
  revoke_restored_cents INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reverse_revocations (
  id INTEGER PRIMARY KEY,
  reversal_key TEXT NOT NULL UNIQUE REFERENCES ledger_entries(entry_key),
  reversal_id INTEGER NOT NULL,
  target_entry_key TEXT NOT NULL,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  restored_cents INTEGER NOT NULL,
  settle_run_id INTEGER,
  run_id TEXT NOT NULL,
  revoked_at TEXT NOT NULL
);

-- 撤销恢复证明（只读证据）：每次撤销恰好一行，记录冲正键、原单敞口前后、
-- 账户余额前后。与撤销事件同事务写入，任一失败整笔回滚。
CREATE TABLE IF NOT EXISTS revoke_restore_proofs (
  id INTEGER PRIMARY KEY,
  revocation_id INTEGER NOT NULL UNIQUE REFERENCES reverse_revocations(id),
  run_id TEXT NOT NULL,
  reversal_key TEXT NOT NULL,
  target_entry_key TEXT NOT NULL,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  restored_cents INTEGER NOT NULL,
  exposure_before_cents INTEGER NOT NULL,
  exposure_after_cents INTEGER NOT NULL,
  balance_before_cents INTEGER NOT NULL,
  balance_after_cents INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reverse_revocations_target
  ON reverse_revocations(target_entry_key);
CREATE INDEX IF NOT EXISTS idx_revoke_restore_proofs_target
  ON revoke_restore_proofs(target_entry_key);
"""


def migrate_db(conn) -> None:
    """为已存在的库补「已撤销」冲正快照列（幂等）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ledger_entries)").fetchall()}
    if "revoked_at" not in cols:
        conn.execute("ALTER TABLE ledger_entries ADD COLUMN revoked_at TEXT")
    if "revoked_run_id" not in cols:
        conn.execute("ALTER TABLE ledger_entries ADD COLUMN revoked_run_id TEXT")
    if "revoked_cents" not in cols:
        conn.execute("ALTER TABLE ledger_entries ADD COLUMN revoked_cents INTEGER")
    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(verify_reports)").fetchall()}
    if "revoke_count" not in rcols:
        conn.execute(
            "ALTER TABLE verify_reports ADD COLUMN revoke_count INTEGER NOT NULL DEFAULT 0"
        )
    if "revoke_restored_cents" not in rcols:
        conn.execute(
            "ALTER TABLE verify_reports ADD COLUMN revoke_restored_cents INTEGER NOT NULL DEFAULT 0"
        )


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        migrate_db(conn)
        conn.commit()
        n = conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        if n:
            return
        seed_path = FIXTURES_DIR / "seed.json"
        data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        for a in data["accounts"]:
            conn.execute(
                "INSERT INTO accounts(id, code, name, currency, balance_cents) VALUES (?, ?, ?, ?, 0)",
                (a["id"], a["code"], a["name"], a["currency"]),
            )
        for e in data["forward_entries"]:
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, '正向', ?, ?, NULL, ?, '待归并', ?)
                """,
                (
                    e["id"],
                    e["entry_key"],
                    e["account_id"],
                    e["amount_cents"],
                    e.get("biz_ref", ""),
                    e["booked_at"],
                    e.get("note", ""),
                ),
            )
        # 冲正 id 与正向可能重叠，用独立自增：先取 max
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM ledger_entries").fetchone()["m"]
        for i, e in enumerate(data["reversal_entries"], start=1):
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, '冲正', ?, ?, ?, ?, '待归并', ?)
                """,
                (
                    max_id + i,
                    e["entry_key"],
                    e["account_id"],
                    e["amount_cents"],
                    e.get("biz_ref", ""),
                    e.get("target_entry_key"),
                    e["booked_at"],
                    e.get("note", ""),
                ),
            )
        conn.commit()
