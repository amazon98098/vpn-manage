"""Database engine and session helpers."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, USABLE_SUBSCRIPTION_STATUSES

_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def _table_cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}


def _migrate_sqlite(engine) -> None:
    """Additive SQLite migrations for existing databases."""
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
        tables = {r[0] for r in rows}
        alters: list[str] = []

        if "renew_jobs" in tables:
            cols = _table_cols(conn, "renew_jobs")
            if "kind" not in cols:
                alters.append(
                    "ALTER TABLE renew_jobs ADD COLUMN kind VARCHAR(16) DEFAULT 'renew'"
                )
            if "last_reminder_at" not in cols:
                alters.append(
                    "ALTER TABLE renew_jobs ADD COLUMN last_reminder_at DATETIME"
                )
            if "vendor_id" not in cols:
                alters.append(
                    "ALTER TABLE renew_jobs ADD COLUMN vendor_id INTEGER"
                )
            if "plan_id" not in cols:
                alters.append(
                    "ALTER TABLE renew_jobs ADD COLUMN plan_id INTEGER"
                )

        if "customer_profiles" in tables:
            pcols = _table_cols(conn, "customer_profiles")
            if "wallet_toman" not in pcols:
                alters.append(
                    "ALTER TABLE customer_profiles ADD COLUMN wallet_toman INTEGER DEFAULT 0"
                )
            if "created_at" not in pcols:
                alters.append(
                    "ALTER TABLE customer_profiles ADD COLUMN created_at DATETIME"
                )
            if "updated_at" not in pcols:
                alters.append(
                    "ALTER TABLE customer_profiles ADD COLUMN updated_at DATETIME"
                )
            if "rate_tier" not in pcols:
                alters.append(
                    "ALTER TABLE customer_profiles ADD COLUMN rate_tier "
                    "VARCHAR(16) DEFAULT 'default'"
                )
            if "referred_by_telegram_id" not in pcols:
                alters.append(
                    "ALTER TABLE customer_profiles ADD COLUMN "
                    "referred_by_telegram_id BIGINT"
                )

        if "price_plans" in tables:
            ppcols = _table_cols(conn, "price_plans")
            if "price_friends_toman" not in ppcols:
                alters.append(
                    "ALTER TABLE price_plans ADD COLUMN price_friends_toman INTEGER"
                )
            if "price_colleagues_toman" not in ppcols:
                alters.append(
                    "ALTER TABLE price_plans ADD COLUMN price_colleagues_toman INTEGER"
                )

        if "education_lessons" in tables:
            elcols = _table_cols(conn, "education_lessons")
            if "media_type" not in elcols:
                alters.append(
                    "ALTER TABLE education_lessons ADD COLUMN media_type VARCHAR(16)"
                )
            if "media_file_id" not in elcols:
                alters.append(
                    "ALTER TABLE education_lessons ADD COLUMN media_file_id VARCHAR(256)"
                )
            if "media_caption" not in elcols:
                alters.append(
                    "ALTER TABLE education_lessons ADD COLUMN media_caption TEXT"
                )

        if "customer_orders" in tables:
            ocols = _table_cols(conn, "customer_orders")
            order_alters = {
                "instructions_at": "ALTER TABLE customer_orders ADD COLUMN instructions_at DATETIME",
                "receipt_reminded_at": "ALTER TABLE customer_orders ADD COLUMN receipt_reminded_at DATETIME",
                "paid_at": "ALTER TABLE customer_orders ADD COLUMN paid_at DATETIME",
                "pay_source": "ALTER TABLE customer_orders ADD COLUMN pay_source VARCHAR(32)",
                "variz_payment_id": "ALTER TABLE customer_orders ADD COLUMN variz_payment_id VARCHAR(64)",
                "checkout_url": "ALTER TABLE customer_orders ADD COLUMN checkout_url VARCHAR(512)",
                "gateway_invoice_id": "ALTER TABLE customer_orders ADD COLUMN gateway_invoice_id VARCHAR(64)",
                "pay_card_number": "ALTER TABLE customer_orders ADD COLUMN pay_card_number VARCHAR(32)",
                "pay_card_holder": "ALTER TABLE customer_orders ADD COLUMN pay_card_holder VARCHAR(128)",
                "wallet_applied_toman": "ALTER TABLE customer_orders ADD COLUMN wallet_applied_toman INTEGER DEFAULT 0",
                "expires_at": "ALTER TABLE customer_orders ADD COLUMN expires_at DATETIME",
                "reminder_count": "ALTER TABLE customer_orders ADD COLUMN reminder_count INTEGER DEFAULT 0",
                "last_reminder_at": "ALTER TABLE customer_orders ADD COLUMN last_reminder_at DATETIME",
                "plan_mode": "ALTER TABLE customer_orders ADD COLUMN plan_mode VARCHAR(16)",
                "admin_hidden": (
                    "ALTER TABLE customer_orders ADD COLUMN admin_hidden "
                    "BOOLEAN DEFAULT 0"
                ),
            }
            for col, stmt in order_alters.items():
                if col not in ocols:
                    alters.append(stmt)

        if "support_tickets" in tables:
            tcols = _table_cols(conn, "support_tickets")
            if "admin_hidden" not in tcols:
                alters.append(
                    "ALTER TABLE support_tickets ADD COLUMN admin_hidden "
                    "BOOLEAN DEFAULT 0"
                )

        if "vendors" in tables:
            vcols = _table_cols(conn, "vendors")
            if "info_refresh_strategy" not in vcols:
                alters.append(
                    "ALTER TABLE vendors ADD COLUMN info_refresh_strategy "
                    "VARCHAR(32) DEFAULT 'automatic'"
                )

        if "subscriptions" in tables:
            scols = _table_cols(conn, "subscriptions")
            sub_alters = {
                "used_traffic_mb": "ALTER TABLE subscriptions ADD COLUMN used_traffic_mb FLOAT",
                "subscription_url": "ALTER TABLE subscriptions ADD COLUMN subscription_url TEXT",
                "last_refresh_method": "ALTER TABLE subscriptions ADD COLUMN last_refresh_method VARCHAR(32)",
                "product_name": "ALTER TABLE subscriptions ADD COLUMN product_name VARCHAR(64)",
                "created_at": "ALTER TABLE subscriptions ADD COLUMN created_at DATETIME",
                "last_userinfo_at": "ALTER TABLE subscriptions ADD COLUMN last_userinfo_at DATETIME",
                "last_days_alert_at": "ALTER TABLE subscriptions ADD COLUMN last_days_alert_at DATETIME",
                "last_traffic_alert_at": "ALTER TABLE subscriptions ADD COLUMN last_traffic_alert_at DATETIME",
                "vendor_alert_at": "ALTER TABLE subscriptions ADD COLUMN vendor_alert_at DATETIME",
                "last_renewed_at": "ALTER TABLE subscriptions ADD COLUMN last_renewed_at DATETIME",
            }
            for col, stmt in sub_alters.items():
                if col not in scols:
                    alters.append(stmt)

        if "customer_service_links" in tables:
            lcols = _table_cols(conn, "customer_service_links")
            if "subscription_id" not in lcols:
                alters.append(
                    "ALTER TABLE customer_service_links ADD COLUMN subscription_id INTEGER"
                )
            if "vendor_id" not in lcols:
                alters.append(
                    "ALTER TABLE customer_service_links ADD COLUMN vendor_id INTEGER"
                )

        for stmt in alters:
            conn.execute(text(stmt))

        if "customer_profiles" in tables:
            pcols = _table_cols(conn, "customer_profiles")
            if "created_at" in pcols:
                conn.execute(
                    text(
                        "UPDATE customer_profiles SET created_at = CURRENT_TIMESTAMP "
                        "WHERE created_at IS NULL"
                    )
                )
            if "updated_at" in pcols:
                conn.execute(
                    text(
                        "UPDATE customer_profiles SET updated_at = CURRENT_TIMESTAMP "
                        "WHERE updated_at IS NULL"
                    )
                )

        # Separate vendor-warnings from local almost-out (one-shot backfill).
        if "subscriptions" in tables and "incoming_messages" in tables:
            scols = _table_cols(conn, "subscriptions")
            if "vendor_alert_at" in scols:
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_subscriptions_vendor_alert_at "
                        "ON subscriptions(vendor_alert_at)"
                    )
                )
                flag = conn.execute(
                    text(
                        "SELECT value FROM app_state "
                        "WHERE key = 'vendor_alert_at_backfill_v2'"
                    )
                ).fetchone()
                if flag is None or (flag[0] or "") != "1":
                    import json as _json

                    alert_rows = conn.execute(
                        text(
                            "SELECT parsed_json, received_at FROM incoming_messages "
                            "WHERE kind IN ('alert_days', 'alert_mb') "
                            "AND parsed_json IS NOT NULL "
                            "ORDER BY received_at ASC"
                        )
                    ).fetchall()
                    latest_alert: dict[str, str] = {}
                    for parsed_json, received_at in alert_rows:
                        try:
                            data = _json.loads(parsed_json or "")
                        except Exception:
                            continue
                        uname = str((data or {}).get("username") or "").strip()
                        if not uname or received_at is None:
                            continue
                        latest_alert[uname] = str(received_at)

                    renew_rows = conn.execute(
                        text(
                            "SELECT parsed_json, received_at FROM incoming_messages "
                            "WHERE kind IN ('renew_ok', 'create_ok') "
                            "AND parsed_json IS NOT NULL "
                            "ORDER BY received_at ASC"
                        )
                    ).fetchall()
                    latest_renew: dict[str, str] = {}
                    for parsed_json, received_at in renew_rows:
                        try:
                            data = _json.loads(parsed_json or "")
                        except Exception:
                            continue
                        uname = str((data or {}).get("username") or "").strip()
                        if not uname or received_at is None:
                            continue
                        latest_renew[uname] = str(received_at)

                    for uname, ts in latest_alert.items():
                        renew_ts = latest_renew.get(uname)
                        # Skip alerts already superseded by a later vendor renew/create.
                        if renew_ts and str(renew_ts) > str(ts):
                            conn.execute(
                                text(
                                    "UPDATE subscriptions SET vendor_alert_at = NULL "
                                    "WHERE username = :u"
                                ),
                                {"u": uname},
                            )
                            continue
                        conn.execute(
                            text(
                                "UPDATE subscriptions SET vendor_alert_at = :ts "
                                "WHERE username = :u AND "
                                "(vendor_alert_at IS NULL OR vendor_alert_at < :ts)"
                            ),
                            {"ts": ts, "u": uname},
                        )

                    # Also clear from completed renew jobs after the alert time.
                    job_rows = conn.execute(
                        text(
                            "SELECT username, completed_at, updated_at FROM renew_jobs "
                            "WHERE status = 'completed' AND kind IN ('renew', 'create')"
                        )
                    ).fetchall()
                    for uname, completed_at, updated_at in job_rows:
                        done = completed_at or updated_at
                        if not uname or not done:
                            continue
                        conn.execute(
                            text(
                                "UPDATE subscriptions SET vendor_alert_at = NULL, "
                                "status = CASE WHEN status = 'warning' THEN 'active' "
                                "ELSE status END "
                                "WHERE username = :u AND vendor_alert_at IS NOT NULL "
                                "AND vendor_alert_at < :done"
                            ),
                            {"u": uname, "done": str(done)},
                        )

                    conn.execute(
                        text(
                            "UPDATE subscriptions SET status = 'active' "
                            "WHERE status = 'warning' AND vendor_alert_at IS NULL"
                        )
                    )
                    if "app_state" in tables:
                        conn.execute(
                            text(
                                "INSERT INTO app_state(key, value) VALUES "
                                "('vendor_alert_at_backfill_v2', '1') "
                                "ON CONFLICT(key) DO UPDATE SET value = '1'"
                            )
                        )

        # Drop retired subscription columns (kept vendor_service_id + subscription_url).
        if "subscriptions" in tables:
            scols = _table_cols(conn, "subscriptions")
            conn.execute(text("DROP INDEX IF EXISTS ix_subscriptions_customer_code"))
            for col in (
                "customer_code",
                "location",
                "last_refresh_at",
                "last_refresh_error",
                "last_alert_type",
                "last_alert_at",
                "last_updated",
                "remaining_days",
            ):
                if col in scols:
                    conn.execute(text(f"ALTER TABLE subscriptions DROP COLUMN {col}"))

            # Backfill created_at for rows added before the column existed.
            scols = _table_cols(conn, "subscriptions")
            if "created_at" in scols:
                conn.execute(
                    text(
                        "UPDATE subscriptions SET created_at = CURRENT_TIMESTAMP "
                        "WHERE created_at IS NULL"
                    )
                )

        if "subscription_ownership_events" in tables:
            conn.execute(text("DROP TABLE IF EXISTS subscription_ownership_events"))

        # Old vendor+username UNIQUE (sqlite autoindex) blocks username reuse.
        if "subscriptions" in tables:
            _drop_vendor_username_unique(conn)

        # Partial unique: one usable subscription per username globally (case-insensitive).
        usable = ",".join(f"'{s}'" for s in sorted(USABLE_SUBSCRIPTION_STATUSES))
        conn.execute(
            text("DROP INDEX IF EXISTS uq_subscriptions_usable_username")
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_usable_username_ci "
                f"ON subscriptions(lower(username)) WHERE status IN ({usable})"
            )
        )
        # One local owner per subscription row when subscription_id is set.
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_link_subscription_id "
                "ON customer_service_links(subscription_id) "
                "WHERE subscription_id IS NOT NULL"
            )
        )


def _drop_vendor_username_unique(conn) -> None:
    """Rebuild subscriptions without UNIQUE(vendor_id, username) if still present."""
    indexes = list(conn.execute(text("PRAGMA index_list(subscriptions)")))
    has_legacy = False
    for row in indexes:
        name = row[1]
        unique = row[2]
        origin = row[3] if len(row) > 3 else ""
        if not unique:
            continue
        cols = [r[2] for r in conn.execute(text(f"PRAGMA index_info('{name}')"))]
        if cols == ["vendor_id", "username"] or (
            name == "uq_vendor_username" or name == "sqlite_autoindex_subscriptions_1"
        ):
            has_legacy = True
            break
    if not has_legacy:
        return

    info = list(conn.execute(text("PRAGMA table_info(subscriptions)")).fetchall())
    cols = [r[1] for r in info]
    col_csv = ", ".join(cols)
    defs = []
    for _cid, name, ctype, notnull, dflt, pk in info:
        piece = f"{name} {ctype or 'TEXT'}"
        if pk:
            piece += " PRIMARY KEY"
        elif notnull:
            piece += " NOT NULL"
        if dflt is not None:
            piece += f" DEFAULT {dflt}"
        defs.append(piece)
    conn.execute(text("PRAGMA foreign_keys=OFF"))
    conn.execute(text(f"CREATE TABLE subscriptions__new ({', '.join(defs)})"))
    conn.execute(
        text(
            f"INSERT INTO subscriptions__new ({col_csv}) SELECT {col_csv} FROM subscriptions"
        )
    )
    conn.execute(text("DROP TABLE subscriptions"))
    conn.execute(text("ALTER TABLE subscriptions__new RENAME TO subscriptions"))
    # Recreate non-unique helpful indexes.
    conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_subscriptions_vendor_id ON subscriptions(vendor_id)")
    )
    conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_subscriptions_username ON subscriptions(username)")
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_subscriptions_vendor_service_id "
            "ON subscriptions(vendor_service_id)"
        )
    )
    conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_subscriptions_status ON subscriptions(status)")
    )
    conn.execute(text("PRAGMA foreign_keys=ON"))


def init_db(db_url: str) -> None:
    global _engine, _SessionLocal
    connect_args: dict = {}
    engine_kwargs: dict = {"future": True}
    if db_url.startswith("sqlite"):
        # Long almost-out / userinfo scans must not freeze other readers/writers.
        connect_args = {"check_same_thread": False, "timeout": 60}
        engine_kwargs["connect_args"] = connect_args
    _engine = create_engine(db_url, **engine_kwargs)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    Base.metadata.create_all(_engine)
    if db_url.startswith("sqlite"):
        with _engine.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA busy_timeout=60000"))
        _migrate_sqlite(_engine)


@contextmanager
def get_session() -> Iterator[Session]:
    if _SessionLocal is None:
        raise RuntimeError("Database is not initialized")
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
