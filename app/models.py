"""نماذج PostgreSQL الخاصة بالمسارات والطلبات وسجل التدقيق."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Numeric, String, Text, false, func, true
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """قاعدة نماذج SQLAlchemy."""


class Currency(str, enum.Enum):
    SYP = "SYP"
    USD = "USD"
    USDT = "USDT"


class RouteCode(str, enum.Enum):
    SYP_TO_USD = "SYP_TO_USD"
    USD_TO_SYP = "USD_TO_SYP"
    SYP_TO_USDT = "SYP_TO_USDT"
    USD_TO_USDT = "USD_TO_USDT"
    USDT_TO_SYP = "USDT_TO_SYP"
    USDT_TO_USD = "USDT_TO_USD"


class RequestStatus(str, enum.Enum):
    AWAITING_REFERENCE = "AWAITING_REFERENCE"
    AWAITING_AMOUNT = "AWAITING_AMOUNT"
    AWAITING_PAYOUT_ADDRESS = "AWAITING_PAYOUT_ADDRESS"
    AWAITING_USER_REVIEW = "AWAITING_USER_REVIEW"
    PENDING_PAYMENT = "PENDING_PAYMENT"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    PENDING_REVIEW = "PENDING_REVIEW"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class AuditAction(str, enum.Enum):
    RATE_UPDATED = "RATE_UPDATED"
    MINIMUM_UPDATED = "MINIMUM_UPDATED"
    ETA_UPDATED = "ETA_UPDATED"
    ROUTE_UPDATED = "ROUTE_UPDATED"
    REQUEST_CREATED = "REQUEST_CREATED"
    REQUEST_SUBMITTED = "REQUEST_SUBMITTED"
    REQUEST_PAYMENT_STARTED = "REQUEST_PAYMENT_STARTED"
    REQUEST_PAYMENT_REPORTED = "REQUEST_PAYMENT_REPORTED"
    REQUEST_EXPIRED = "REQUEST_EXPIRED"
    REQUEST_COMPLETED = "REQUEST_COMPLETED"
    REQUEST_REJECTED = "REQUEST_REJECTED"
    REQUEST_CANCELLED = "REQUEST_CANCELLED"
    WITHDRAWAL_HOLD = "WITHDRAWAL_HOLD"
    WITHDRAWAL_PAID = "WITHDRAWAL_PAID"
    WITHDRAWAL_RELEASED = "WITHDRAWAL_RELEASED"
    USER_BANNED = "USER_BANNED"
    USER_UNBANNED = "USER_UNBANNED"


class ExchangeRoute(Base):
    """إعداد الدفع لكل واحد من مسارات التحويل الستة."""

    __tablename__ = "exchange_routes"

    code: Mapped[RouteCode] = mapped_column(Enum(RouteCode, native_enum=False), primary_key=True)
    from_currency: Mapped[Currency] = mapped_column(Enum(Currency, native_enum=False), nullable=False)
    to_currency: Mapped[Currency] = mapped_column(Enum(Currency, native_enum=False), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    rate_value: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    fee_percentage: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False, default=Decimal("0"))
    minimum_usd: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    payment_address: Mapped[str | None] = mapped_column(String(256), nullable=True)
    barcode_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ExchangeRequest(Base):
    """طلب تحويل يدوي؛ يثبت السعر والبيانات لحظة إنشاء الطلب."""

    __tablename__ = "exchange_requests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    public_id: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    route_code: Mapped[RouteCode] = mapped_column(
        Enum(RouteCode, native_enum=False), ForeignKey("exchange_routes.code"), index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(256), nullable=False)

    from_currency: Mapped[Currency] = mapped_column(Enum(Currency, native_enum=False), nullable=False)
    to_currency: Mapped[Currency] = mapped_column(Enum(Currency, native_enum=False), nullable=False)
    payment_address_snapshot: Mapped[str] = mapped_column(String(256), nullable=False)
    barcode_file_id_snapshot: Mapped[str | None] = mapped_column(String(256), nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payment_proof_file_id: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
    )
    payment_proof_type: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )
    payment_reported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    from_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    to_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    payout_address: Mapped[str | None] = mapped_column(String(256), nullable=True)
    crypto_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payment_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    review_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


    syp_per_usd_snapshot: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    usdt_per_usd_snapshot: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    minimum_usd_snapshot: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    rate_snapshot: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    fee_percentage_snapshot: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    eta_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[RequestStatus] = mapped_column(
        Enum(RequestStatus, native_enum=False), default=RequestStatus.AWAITING_REFERENCE, index=True
    )
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_exchange_requests_user_status", "user_id", "status"),
        Index(
            "uq_exchange_requests_payment_reference",
            "payment_reference",
            unique=True,
            postgresql_where=payment_reference.is_not(None),
        ),
    )


class BotUser(Base):
    """مستخدم مسجل لحظر الحسابات والإحصاءات والإرسال الجماعي."""

    __tablename__ = "bot_users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(256), nullable=False)
    is_banned: Mapped[bool] = mapped_column(default=False, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, index=True)


class WithdrawalStatus(str, enum.Enum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PAID = "PAID"
    CANCELLED = "CANCELLED"


class ReferralProfile(Base):
    """ملف إحالة مستقل لتفادي كسر جداول المستخدمين القديمة."""

    __tablename__ = "referral_profiles"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    referral_code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    referrer_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    referral_count: Mapped[int] = mapped_column(default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ReferralEvent(Base):
    """إحالة مؤكدة مرة واحدة؛ تعطيلها نهائي وتحفظ تفاصيل المكافأة."""

    __tablename__ = "referral_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    referrer_user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    referred_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # False is intentional for migrated rows; new events set it explicitly to True.
    eligible_for_levels: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False, index=True)
    reward_granted: Mapped[Decimal] = mapped_column(Numeric(24, 8), default=Decimal("0"), server_default="0", nullable=False)
    reward_reversed: Mapped[Decimal] = mapped_column(Numeric(24, 8), default=Decimal("0"), server_default="0", nullable=False)


class Wallet(Base):
    """محفظة مكافآت بالدولار مع رصيد متاح ومحجوز للسحب."""

    __tablename__ = "wallets"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    available_balance: Mapped[Decimal] = mapped_column(Numeric(24, 8), default=Decimal("0"), nullable=False)
    reserved_balance: Mapped[Decimal] = mapped_column(Numeric(24, 8), default=Decimal("0"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class WalletLedgerEntry(Base):
    """سجل غير قابل للتكرار لحركات المحفظة."""

    __tablename__ = "wallet_ledger"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class WithdrawalAccount(Base):
    """بيانات حساب السحب التي يملكها المستخدم."""

    __tablename__ = "withdrawal_accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    account_value: Mapped[str] = mapped_column(String(256), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WithdrawalRequest(Base):
    """طلب سحب مستقل عن طلبات تحويل العملات."""

    __tablename__ = "withdrawal_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(12), unique=True, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    method_snapshot: Mapped[str] = mapped_column(String(32), nullable=False)
    account_snapshot: Mapped[str] = mapped_column(String(256), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    status: Mapped[WithdrawalStatus] = mapped_column(Enum(WithdrawalStatus, native_enum=False), default=WithdrawalStatus.PENDING_REVIEW, index=True, nullable=False)
    review_not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class WalletCreditLot(Base):
    """رصيد كل قيد ائتماني مستقل، لاستخدام FIFO الآمن عند الحجز والعكس."""

    __tablename__ = "wallet_credit_lots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    ledger_entry_id: Mapped[int] = mapped_column(ForeignKey("wallet_ledger.id"), unique=True, index=True, nullable=False)
    original_amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    remaining_amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class WalletWithdrawalAllocation(Base):
    """تخصيص جزء من كل credit lot لسحب؛ يبقى قابلاً للإرجاع عند الرفض."""

    __tablename__ = "wallet_withdrawal_allocations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    withdrawal_request_id: Mapped[int] = mapped_column(ForeignKey("withdrawal_requests.id"), index=True, nullable=False)
    credit_lot_id: Mapped[int] = mapped_column(ForeignKey("wallet_credit_lots.id"), index=True, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ExchangeSetting(Base):
    """إعداد عام قابل للتعديل من لوحة التحكم."""

    __tablename__ = "exchange_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AuditLog(Base):
    """سجل تدقيق للأفعال الحساسة، لا يُعرض للعملاء."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    action: Mapped[AuditAction] = mapped_column(Enum(AuditAction, native_enum=False), index=True)
    request_public_id: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
