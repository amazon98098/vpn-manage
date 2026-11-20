"""SQLAlchemy models for Phase 1 (alerts + subscriptions)."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    telegram_username: Mapped[str] = mapped_column(String(64), unique=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # subscription_url | provider_bot | automatic
    info_refresh_strategy: Mapped[str] = mapped_column(String(32), default="automatic")

    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="vendor")
    messages: Mapped[list[IncomingMessage]] = relationship(back_populates="vendor")


# Statuses that still occupy a username for new-purchase uniqueness.
USABLE_SUBSCRIPTION_STATUSES = frozenset(
    {"active", "warning", "expired", "unknown", "disabled", "volume_exhausted"}
)


class Subscription(Base):
    """Provider inventory row. Username is searchable/reusable; PK is permanent identity.

    Usable-username uniqueness is enforced via a partial SQLite index in migrations
    (historical removed/deleted rows may reuse the same username as a new row).
    """

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    vendor_service_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Our catalog label only (e.g. «۵۵ گیگ یکماهه») — never NetPlus priced product text.
    product_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    traffic_limit_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    used_traffic_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    remaining_traffic_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Full expire instant (day + time) from subscription-userinfo unix expire.
    expiration_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    subscription_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_refresh_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Last successful HTTP subscription-userinfo apply (for TTL).
    last_userinfo_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Local almost-out customer alert cooldowns (Phase 6).
    last_days_alert_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_traffic_alert_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set only when vendor bot sends alert_days / alert_mb (vendor-warnings page).
    # Local almost-out / userinfo checks must never set this.
    vendor_alert_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # Last successful renew/create from our queue (admin bot, panel, or customer).
    # Used to drop rows from vendor-warnings / almost-out for a few days after renew.
    last_renewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # Our local row creation time (not a NetPlus field).
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    vendor: Mapped[Vendor] = relationship(back_populates="subscriptions")


class IncomingMessage(Base):
    __tablename__ = "incoming_messages"
    __table_args__ = (
        UniqueConstraint("vendor_id", "telegram_message_id", name="uq_vendor_msg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger)
    message_text: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    parsed_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    vendor: Mapped[Vendor] = relationship(back_populates="messages")


class AppState(Base):
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class PricePlan(Base):
    """Reseller sell prices (your prices), independent of NetPlus cost."""

    __tablename__ = "price_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(128))
    duration_days: Mapped[int] = mapped_column(Integer, default=31)
    traffic_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_toman: Mapped[int] = mapped_column(Integer, default=0)
    vendor_cost_toman: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CustomerProfile(Base):
    """Telegram customer using the shop bot."""

    __tablename__ = "customer_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    wallet_toman: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WalletLedger(Base):
    """Customer wallet credit/debit history (NetPlus-like)."""

    __tablename__ = "wallet_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount_toman: Mapped[int] = mapped_column(Integer)  # +credit / -debit
    balance_after: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(32), default="charge")  # charge|spend|adjust
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CustomerServiceLink(Base):
    """Maps a customer Telegram account to a local subscription (ownership)."""

    __tablename__ = "customer_service_links"
    __table_args__ = (
        UniqueConstraint("telegram_id", "service_username", name="uq_customer_service"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    service_username: Mapped[str] = mapped_column(String(64), index=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=True, index=True
    )
    vendor_id: Mapped[int | None] = mapped_column(
        ForeignKey("vendors.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CustomerOrder(Base):
    """Customer buy/renew order paid to reseller (your card), then fulfilled via NetPlus."""

    __tablename__ = "customer_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="create")  # create|renew
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("price_plans.id"), nullable=True)
    # renew: current|change; create/wallet: null
    plan_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    service_username: Mapped[str] = mapped_column(String(64), index=True)
    price_toman: Mapped[int] = mapped_column(Integer, default=0)
    wallet_applied_toman: Mapped[int] = mapped_column(Integer, default=0)
    amount_rial: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="awaiting_payment", index=True)
    # draft | awaiting_payment | receipt_window | awaiting_review | approved | rejected | fulfilling | done | cancelled
    renew_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    receipt_reminded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    reminder_count: Mapped[int] = mapped_column(Integer, default=0)
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pay_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # auto_amount | admin_manual | customer_receipt | variz | aban
    variz_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    gateway_invoice_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    pay_card_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pay_card_holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    checkout_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Hidden from admin request lists (does not cancel the customer order).
    admin_hidden: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # draft | awaiting_payment | receipt_window | awaiting_review | approved | rejected
    # | fulfilling | done | cancelled | expired


class BotMessage(Base):
    """Admin-editable customer-facing copy keyed by stable string id."""

    __tablename__ = "bot_messages"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EducationCategory(Base):
    """Top-level (or nested) education menu section."""

    __tablename__ = "education_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("education_categories.id"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(128))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EducationLesson(Base):
    """Lesson body shown to customers under a category."""

    __tablename__ = "education_lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("education_categories.id"), index=True
    )
    title: Mapped[str] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SupportTicket(Base):
    """Support / rejection conversation thread (often tied to an order)."""

    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_orders.id"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="support")
    # support | rejection | order
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    # open | awaiting_customer | awaiting_admin | closed
    subject: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Hidden from admin request lists (ticket stays in customer history).
    admin_hidden: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class SupportMessage(Base):
    """Single message inside a support ticket."""

    __tablename__ = "support_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("support_tickets.id"), index=True)
    sender: Mapped[str] = mapped_column(String(16), default="customer")
    # customer | admin | system
    admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RenewJob(Base):
    """Unified vendor order job: renew or create + payment session (Phase 2/3)."""

    __tablename__ = "renew_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="renew", index=True)  # renew|create
    username: Mapped[str] = mapped_column(String(64), index=True)
    # Target provider row; defaults to NetPlus until another adapter exists.
    vendor_id: Mapped[int | None] = mapped_column(
        ForeignKey("vendors.id"), nullable=True, index=True
    )
    vendor_service_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=True, index=True
    )
    plan_mode: Mapped[str] = mapped_column(String(16), default="current")  # current|change|create
    # Optional sell PricePlan id — used to auto-pick NetPlus package GB without admin click.
    plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("price_plans.id"), nullable=True, index=True
    )
    duration_callback: Mapped[str | None] = mapped_column(String(64), nullable=True)
    package_callback: Mapped[str | None] = mapped_column(String(128), nullable=True)
    packages_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    amount_rial: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    card_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    card_holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    receipt_callback: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payment_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    instructions_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
