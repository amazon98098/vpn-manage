"""Retry paid renew for alam_chehreh_155_1; clear stuck create job."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "vpn_manager.db"


def main() -> None:
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")
    now = datetime.utcnow().isoformat(sep=" ")

    con.execute(
        "UPDATE renew_jobs SET status='failed', error=?, updated_at=?, completed_at=? "
        "WHERE id=13 AND status IN ('queued','driving')",
        ("cancelled_stale_create_blocks_queue", now, now),
    )
    print("j13", dict(con.execute("SELECT id,status,error FROM renew_jobs WHERE id=13").fetchone()))

    row = con.execute("SELECT * FROM renew_jobs WHERE id=12").fetchone()
    if not row:
        raise SystemExit("job 12 missing")
    row = dict(row)

    # Avoid duplicate open retries
    open_alam = con.execute(
        "SELECT id,status FROM renew_jobs WHERE username=? AND status IN "
        "('queued','driving','awaiting_payment','awaiting_package',"
        "'receipt_window','awaiting_receipt_confirm','awaiting_auto_approve')",
        (row["username"],),
    ).fetchall()
    if open_alam:
        print("already open", [dict(r) for r in open_alam])
        con.commit()
        return

    cur = con.execute(
        """
        INSERT INTO renew_jobs (
            username, vendor_service_id, subscription_id, plan_mode,
            duration_callback, package_callback, packages_json, status,
            amount_rial, card_number, card_holder, receipt_callback,
            payment_message_id, instructions_at, error, requested_by,
            created_at, updated_at, completed_at, kind, last_reminder_at, vendor_id
        ) VALUES (
            ?,?,?,?,?,?,NULL,'queued',
            NULL,NULL,NULL,NULL,
            NULL,NULL,NULL,?,
            ?,?,NULL,'renew',NULL,?
        )
        """,
        (
            row["username"],
            row["vendor_service_id"],
            row["subscription_id"],
            row["plan_mode"],
            row["duration_callback"],
            row["package_callback"],
            row["requested_by"],
            now,
            now,
            row["vendor_id"],
        ),
    )
    new_id = cur.lastrowid
    con.execute(
        "UPDATE customer_orders SET renew_job_id=?, status='fulfilling', updated_at=?, "
        "note=COALESCE(note,'') || ? WHERE id=12",
        (new_id, now, f"|retry_job:{new_id}"),
    )
    con.commit()
    print("new_job", new_id)
    print(
        "order",
        dict(
            con.execute(
                "SELECT id,status,renew_job_id FROM customer_orders WHERE id=12"
            ).fetchone()
        ),
    )
    con.close()


if __name__ == "__main__":
    main()
