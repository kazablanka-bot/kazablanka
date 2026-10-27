"""منطق التحويل اليدوي وإدارة المسارات والأسعار في PostgreSQL."""

from __future__ import annotations

import asyncio
import aiohttp
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN
from typing import Final

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    AuditAction,
    AuditLog,
    BotUser,
    Currency,
    ExchangeRequest,
    ExchangeRoute,
    ExchangeSetting,
    ReferralEvent,
    ReferralProfile,
    RequestStatus,
    RouteCode,
    Wallet,
    WalletCreditLot,
    WalletLedgerEntry,
    WalletWithdrawalAllocation,
    WithdrawalAccount,
    WithdrawalRequest,
    WithdrawalStatus,
)
from app.static_routes import load_static_routes

SYP_QUANTUM: Final[Decimal] = Decimal("1")
USD_QUANTUM: Final[Decimal] = Decimal("0.01")
USDT_QUANTUM: Final[Decimal] = Decimal("0.01")
MAX_AMOUNT: Final[Decimal] = Decimal("1000000000")
_REFERRAL_LOCK = asyncio.Lock()
_USER_ACTIVITY_WRITE_INTERVAL = timedelta(seconds=60)

ROUTE_DEFAULTS: Final[dict[RouteCode, tuple[Currency, Currency, str]]] = {
    RouteCode.SYP_TO_USD: (Currency.SYP, Currency.USD, "شام كاش ليرة ← شام كاش دولار"),
    RouteCode.USD_TO_SYP: (Currency.USD, Currency.SYP, "شام كاش دولار ← شام كاش ليرة"),
    RouteCode.SYP_TO_USDT: (Currency.SYP, Currency.USDT, "شام كاش ليرة ← USDT"),
    RouteCode.USD_TO_USDT: (Currency.USD, Currency.USDT, "شام كاش دولار ← USDT"),
    RouteCode.USDT_TO_SYP: (Currency.USDT, Currency.SYP, "USDT ← شام كاش ليرة"),
    RouteCode.USDT_TO_USD: (Currency.USDT, Currency.USD, "USDT ← شام كاش دولار"),
}


class DomainError(ValueError):
    """خطأ آمن يمكن عرضه للمستخدم دون كشف تفاصيل داخلية."""

_SP_TODAY_URL = "https://sse.sp-today.com/snapshot"
_SP_TODAY_CACHE: tuple[Decimal, Decimal, datetime] | None = None
_SP_TODAY_CACHE_LOCK = asyncio.Lock()
_SP_TODAY_CACHE_TTL = timedelta(minutes=5)


async def get_sp_today_usd_rates(
    force_refresh: bool = False,
) -> tuple[Decimal, Decimal, datetime]:
    """يعيد شراء وبيع الدولار بالليرة الجديدة ووقت آخر جلب من الليرة اليوم."""
    global _SP_TODAY_CACHE
    now = datetime.now(UTC)
    
    if (
        not force_refresh
        and _SP_TODAY_CACHE is not None
        and now - _SP_TODAY_CACHE[2] < _SP_TODAY_CACHE_TTL
    ):
        return _SP_TODAY_CACHE

    async with _SP_TODAY_CACHE_LOCK:
        now = datetime.now(UTC)
        if (
            not force_refresh
            and _SP_TODAY_CACHE is not None
            and now - _SP_TODAY_CACHE[2] < _SP_TODAY_CACHE_TTL
        ):
            return _SP_TODAY_CACHE

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.get(
                    _SP_TODAY_URL,
                    headers={"Accept": "application/json"},
                ) as response:
                    response.raise_for_status()
                    payload = await response.json(content_type=None)

            row = payload["data"]["currencies"]["USD:damascus"]
            buy_syp = Decimal(str(row["buy"])) / Decimal("100")
            sell_syp = Decimal(str(row["sell"])) / Decimal("100")

            if buy_syp <= 0 or sell_syp <= 0:
                raise ValueError("سعر الدولار غير صالح.")

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            KeyError,
            TypeError,
            ValueError,
            InvalidOperation,
        ) as error:
            raise DomainError(
                "أعد المحاولة بعد قليل."
            ) from error

        _SP_TODAY_CACHE = (
            buy_syp,
            sell_syp,
            datetime.now(UTC),
        )
        return _SP_TODAY_CACHE


async def live_rate_for_route(
    route_code: RouteCode,
    force_refresh: bool = False,
) -> Decimal:

    """سعر الليرة اليوم الملائم للمسار؛ ومسارا الدولار وUSDT على أساس 1:1."""
    if route_code in {
        RouteCode.USD_TO_USDT,
        RouteCode.USDT_TO_USD,
    }:
        return Decimal("1")

    buy_syp, sell_syp, _ = await get_sp_today_usd_rates(
        force_refresh=force_refresh
    )


    if route_code in {
        RouteCode.SYP_TO_USD,
        RouteCode.SYP_TO_USDT,
    }:
        return sell_syp

    if route_code in {
        RouteCode.USD_TO_SYP,
        RouteCode.USDT_TO_SYP,
    }:
        return buy_syp

    raise DomainError("الطلب غير معروف.")


class DuplicatePaymentReferenceError(DomainError):
    """رقم عملية مستخدم سابقاً."""


@dataclass(frozen=True)
class ExchangeRates:
    syp_per_usd: Decimal
    usdt_per_usd: Decimal
    minimum_usd: Decimal
    completion_eta: str


@dataclass(frozen=True)
class DashboardStats:
    pending_review: int
    drafts_in_progress: int
    completed_today: int
    total_users: int = 0
    active_today: int = 0
    completed_total: int = 0
    total_referrals: int = 0
    total_withdrawal_paid: Decimal = Decimal("0")


@dataclass(frozen=True)
class WalletSummary:
    balance: Decimal
    reserved: Decimal
    referrals: int
    referral_code: str
    account_method: str | None = None
    account_value: str | None = None
    withdrawal_minimum: Decimal = Decimal("50")
    referral_level: int = 0
    referral_reward: Decimal = Decimal("0")

    @property
    def referral_level_reward(self) -> Decimal:
        """اسم بديل واضح للـ handler عند عرض مكافأة المستوى الحالي."""
        return self.referral_reward


@dataclass(frozen=True)
class LeaderboardEntry:
    user_id: int
    display_name: str
    referrals: int


def referral_level_for_count(referral_count: int) -> int:
    """يحسب مستوى البرنامج الجديد من الإحالات النشطة المؤهلة فقط."""
    if referral_count >= 101:
        return 4
    if referral_count >= 51:
        return 3
    if referral_count >= 21:
        return 2
    if referral_count >= 0:
        return 1
    return 1


def referral_reward_for_level(settings: Settings, level: int) -> Decimal:
    return {
        1: settings.referral_level_1_reward,
        2: settings.referral_level_2_reward,
        3: settings.referral_level_3_reward,
        4: settings.referral_level_4_reward,
    }.get(level, Decimal("0"))


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """يعيد تاريخاً واعياً بالتوقيت حتى مع SQLite الذي قد يعيد تاريخاً بلا منطقة."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def decimal_from_text(raw_value: str) -> Decimal:
    normalized = raw_value.strip().replace(",", "").replace("٫", ".")
    if not normalized or len(normalized) > 32:
        raise DomainError("أدخل قيمة رقمية صحيحة فقط.")
    try:
        value = Decimal(normalized)
    except InvalidOperation as error:
        raise DomainError("أدخل قيمة رقمية صحيحة فقط.") from error
    if not value.is_finite() or value <= 0 or value > MAX_AMOUNT:
        raise DomainError("القيمة يجب أن تكون أكبر من صفر وضمن الحد المسموح.")
    return value


def decimal_nonnegative_from_text(raw_value: str) -> Decimal:
    """يقرأ قيمة عشرية موجبة أو صفراً، لاستخدام نسبة العمولة."""
    normalized = raw_value.strip().replace(",", "").replace("٫", ".")
    if not normalized or len(normalized) > 32:
        raise DomainError("أدخل نسبة رقمية صحيحة.")
    try:
        value = Decimal(normalized)
    except InvalidOperation as error:
        raise DomainError("أدخل نسبة رقمية صحيحة.") from error
    if not value.is_finite() or value < 0 or value >= 100:
        raise DomainError("يجب أن تكون العمولة بين صفر وأقل من 100٪.")
    return value


def quantum_for(currency: Currency) -> Decimal:
    return {Currency.SYP: SYP_QUANTUM, Currency.USD: USD_QUANTUM, Currency.USDT: USDT_QUANTUM}[currency]


def format_amount(amount: Decimal, currency: Currency) -> str:
    precision = {
        Currency.SYP: 0,
        Currency.USD: 2,
        Currency.USDT: 2,
    }[currency]
    names = {
        Currency.SYP: "ل.س",
        Currency.USD: "دولار",
        Currency.USDT: "USDT",
    }

    display_quantum = {
        Currency.SYP: Decimal("1"),
        Currency.USD: Decimal("0.01"),
        Currency.USDT: Decimal("0.01"),
    }[currency]

    rounded_amount = amount.quantize(
        display_quantum,
        rounding=ROUND_DOWN,

    
    )
    number = f"{rounded_amount:,.{precision}f}"

    if "." in number:
        number = number.rstrip("0").rstrip(".")

    return f"{number} {names[currency]}"

def format_rate(rate: Decimal) -> str:
    """يعرض السعر حتى ثماني منازل عشرية مع حذف الأصفار غير المهمة."""
    return f"{rate:,.8f}".rstrip("0").rstrip(".")


def calculate_quote(
    amount: Decimal,
    from_currency: Currency,
    to_currency: Currency,
    rates: ExchangeRates,
) -> tuple[Decimal, Decimal]:
    """يعيد المبلغ المستلم والمعادل بالدولار، دون عمولات خفية."""
    if from_currency is to_currency:
        raise DomainError("لا يمكن التحويل إلى العملة نفسها.")

    rounded_amount = amount.quantize(
        quantum_for(from_currency),
        rounding=ROUND_DOWN,
    )
    if rounded_amount != amount:
        raise DomainError(
            "مبلغ الإرسال يتجاوز المنازل العشرية المسموح بها لهذه العملة."
        )
    amount = rounded_amount

    if amount <= 0:
        raise DomainError("القيمة صغيرة جداً بعد التقريب.")

    if from_currency is Currency.SYP:
        usd_equivalent = amount / rates.syp_per_usd
    elif from_currency is Currency.USD:
        usd_equivalent = amount
    else:
        usd_equivalent = amount / rates.usdt_per_usd

    if usd_equivalent < rates.minimum_usd:
        raise DomainError(
            f"الحد الأدنى هو {format_amount(rates.minimum_usd, Currency.USD)} أو ما يعادله حسب سعر الصرف."
        )

    if to_currency is Currency.SYP:
        received = usd_equivalent * rates.syp_per_usd
    elif to_currency is Currency.USD:
        received = usd_equivalent
    else:
        received = usd_equivalent * rates.usdt_per_usd

    return (
        received.quantize(quantum_for(to_currency), rounding=ROUND_DOWN),
        usd_equivalent.quantize(USDT_QUANTUM, rounding=ROUND_DOWN),
    )


def minimum_transfer(route_code: RouteCode, minimum_usd: Decimal, rate: Decimal) -> tuple[Decimal, Currency]:
    """يحسب الحد الأدنى بعملة الإرسال وفق سعر المسار الحالي."""
    source_currency = ROUTE_DEFAULTS[route_code][0]
    if source_currency is Currency.SYP:
        raw = minimum_usd * rate
    elif source_currency is Currency.USDT:
        raw = minimum_usd
    else:
        raw = minimum_usd
    return raw.quantize(quantum_for(source_currency), rounding=ROUND_CEILING), source_currency


def calculate_route_quote(amount: Decimal, route_code: RouteCode, rate: Decimal, fee_percentage: Decimal,
                          minimum_usd: Decimal) -> tuple[Decimal, Decimal]:
    """يحسب عرضاً مستقلاً للمسار ويطبق عمولة المسار المحددة صراحة."""
    if rate <= 0 or fee_percentage < 0 or fee_percentage >= 100:
        raise DomainError("إعدادات سعر أو عمولة المسار غير صالحة.")
    from_currency, to_currency, _ = ROUTE_DEFAULTS[route_code]

    rounded_amount = amount.quantize(
        quantum_for(from_currency),
        rounding=ROUND_DOWN,
    )
    if rounded_amount != amount:
        raise DomainError(
            "مبلغ الإرسال يتجاوز المنازل العشرية المسموح بها لهذه العملة."
        )
    amount = rounded_amount


    min_amount, _ = minimum_transfer(route_code, minimum_usd, rate)
    if amount < min_amount:
        raise DomainError(f"الحد الأدنى لهذا الطلب هو {format_amount(min_amount, from_currency)}.")

    if route_code in {RouteCode.SYP_TO_USD, RouteCode.SYP_TO_USDT}:
        gross_received = amount / rate
    elif route_code in {RouteCode.USD_TO_SYP, RouteCode.USDT_TO_SYP}:
        gross_received = amount * rate
    else:
        gross_received = amount

    received = gross_received * (
        Decimal("1") - fee_percentage / Decimal("100")
    )
    usd_equivalent = amount / rate if from_currency is Currency.SYP else amount
    return received.quantize(quantum_for(to_currency), rounding=ROUND_DOWN), usd_equivalent.quantize(USDT_QUANTUM, rounding=ROUND_DOWN)

def calculate_payment_for_target(
    target_amount: Decimal,
    route_code: RouteCode,
    rate: Decimal,
    fee_percentage: Decimal,
    minimum_usd: Decimal,
    usdt_per_usd: Decimal,
) -> tuple[Decimal, Decimal]:
    """يحسب مبلغ الدفع المطلوب لتحقيق مبلغ استلام محدد مسبقاً."""
    if (
        not target_amount.is_finite()
        or target_amount <= 0
        or target_amount > MAX_AMOUNT
    ):
        raise DomainError("مبلغ الاستلام غير صالح.")

    if (
        not rate.is_finite()
        or rate <= 0
        or not fee_percentage.is_finite()
        or fee_percentage < 0
        or fee_percentage >= 100
        or not minimum_usd.is_finite()
        or minimum_usd <= 0
        or not usdt_per_usd.is_finite()
        or usdt_per_usd <= 0
    ):
        raise DomainError("إعدادات السعر أو العمولة أو الحد الأدنى غير صالحة.")

    from_currency, to_currency, _ = ROUTE_DEFAULTS[route_code]

    rounded_target = target_amount.quantize(
        quantum_for(to_currency),
        rounding=ROUND_DOWN,
    )
    if rounded_target != target_amount:
        raise DomainError(
            "مبلغ الاستلام يحتوي منازل عشرية أكثر من المسموح لهذه العملة."
        )

    gross_target = target_amount / (
        Decimal("1") - fee_percentage / Decimal("100")
    )

    if route_code in {RouteCode.SYP_TO_USD, RouteCode.SYP_TO_USDT}:
        raw_payment = gross_target * rate
    elif route_code in {RouteCode.USD_TO_SYP, RouteCode.USDT_TO_SYP}:
        raw_payment = gross_target / rate
    else:
        raw_payment = gross_target
    payment_amount = raw_payment.quantize(
        quantum_for(from_currency),
        rounding=ROUND_CEILING,
    )

    if payment_amount <= 0 or payment_amount > MAX_AMOUNT:
        raise DomainError("مبلغ الدفع المطلوب يتجاوز الحد المسموح.")

    if from_currency is Currency.SYP:
        if route_code is RouteCode.SYP_TO_USD:
            usd_equivalent = payment_amount / rate
        else:
            usd_equivalent = payment_amount / rate / usdt_per_usd
    elif from_currency is Currency.USD:
        usd_equivalent = payment_amount
    else:
        usd_equivalent = payment_amount / usdt_per_usd

    if usd_equivalent < minimum_usd:
        raise DomainError(
            f"الحد الأدنى للدفع يعادل {format_amount(minimum_usd, Currency.USD)} أو ما يعادله."
        )

    return (
        payment_amount,
        usd_equivalent.quantize(USDT_QUANTUM, rounding=ROUND_DOWN),
    )


def calculate_target_breakdown(
    target_amount: Decimal,
    route_code: RouteCode,
    rate: Decimal,
    fee_percentage: Decimal,
    minimum_usd: Decimal,
    usdt_per_usd: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """يعيد مبلغ الدفع وقيمة التحويل والعمولة والصافي الفعلي بعد التقريب."""

    payment_amount, _ = calculate_payment_for_target(
        target_amount=target_amount,
        route_code=route_code,
        rate=rate,
        fee_percentage=fee_percentage,
        minimum_usd=minimum_usd,
        usdt_per_usd=usdt_per_usd,
    )

    from_currency, to_currency, _ = ROUTE_DEFAULTS[route_code]

    if route_code in {RouteCode.SYP_TO_USD, RouteCode.SYP_TO_USDT}:
        gross_raw = payment_amount / rate
    elif route_code in {RouteCode.USD_TO_SYP, RouteCode.USDT_TO_SYP}:
        gross_raw = payment_amount * rate
    else:
        gross_raw = payment_amount

    gross_amount = gross_raw.quantize(
        quantum_for(to_currency),
        rounding=ROUND_DOWN,
    )

    net_amount, _ = calculate_route_quote(
        amount=payment_amount,
        route_code=route_code,
        rate=rate,
        fee_percentage=fee_percentage,
        minimum_usd=minimum_usd,
    )

    fee_amount = gross_amount - net_amount

    return payment_amount, gross_amount, fee_amount, net_amount


def route_rate_label(route_code: RouteCode) -> str:
    return "سعر الطلب (ل.س لكل دولار أو ما يعادله)"


def clean_short_text(raw_value: str, label: str, maximum_length: int = 256) -> str:
    value = " ".join(raw_value.split())
    if not value or len(value) > maximum_length or any(ord(char) < 32 for char in value):
        raise DomainError(f"{label} مطلوب ويجب ألا يتجاوز {maximum_length} محرفاً.")
    return value


class ExchangeService:
    """خدمة المعاملات والإعدادات؛ كل معالج يفتح جلسة مستقلة قصيرة."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    async def bootstrap(self) -> None:
        """ينشئ الإعدادات ويحمّل الأكواد والصور من ملفات assets الثابتة."""
        static_routes = load_static_routes()
        setting_defaults = {
            "syp_per_usd": str(self.settings.default_syp_per_usd),
            "usdt_per_usd": str(self.settings.default_usdt_per_usd),
            "minimum_usd": str(self.settings.minimum_usd),
            "completion_eta": self.settings.completion_eta,
        }
        existing_settings = set((await self.session.scalars(select(ExchangeSetting.key))).all())
        for key, value in setting_defaults.items():
            if key not in existing_settings:
                self.session.add(ExchangeSetting(key=key, value=value))

        existing_routes = set((await self.session.scalars(select(ExchangeRoute.code))).all())
        fee_defaults_initialized = await self.session.get(ExchangeSetting, "route_fee_defaults_initialized")
        for code, (from_currency, to_currency, display_name) in ROUTE_DEFAULTS.items():
            if code not in existing_routes:
                self.session.add(
                    ExchangeRoute(
                        code=code,
                        from_currency=from_currency,
                        to_currency=to_currency,
                        display_name=display_name,
                        rate_value=(self.settings.default_syp_per_usd if from_currency is Currency.SYP or to_currency is Currency.SYP else self.settings.default_usdt_per_usd),
                        fee_percentage=(self.settings.default_usd_to_usdt_fee if code is RouteCode.USD_TO_USDT
                                       else self.settings.default_usdt_to_usd_fee if code is RouteCode.USDT_TO_USD
                                       else Decimal("0")),
                        minimum_usd=self.settings.minimum_usd,
                        payment_address=static_routes[code].payment_address,
                        barcode_file_id=str(static_routes[code].barcode_path),
                        is_enabled=False,
                    )
                )
            else:
                route = await self.get_route(code)
                if route.rate_value is None or route.rate_value <= 0:
                    route.rate_value = self.settings.default_syp_per_usd if Currency.SYP in (from_currency, to_currency) else self.settings.default_usdt_per_usd
                if route.minimum_usd is None or route.minimum_usd <= 0:
                    route.minimum_usd = self.settings.minimum_usd
                if not route.payment_address:
                    route.payment_address = static_routes[code].payment_address
                if not route.barcode_file_id:
                    route.barcode_file_id = str(static_routes[code].barcode_path)
        if fee_defaults_initialized is None:
            await self.session.flush()
            fee_routes = {
                RouteCode.USD_TO_USDT: self.settings.default_usd_to_usdt_fee,
                RouteCode.USDT_TO_USD: self.settings.default_usdt_to_usd_fee,
            }
            for code, fee in fee_routes.items():
                route = await self.get_route(code)
                route.fee_percentage = fee
            self.session.add(ExchangeSetting(key="route_fee_defaults_initialized", value="1"))

        # تصحيح نسخ SQLite القديمة التي كانت تستخدم سعر الليرة للـ USDT.
        # لا نلمس سعراً مخصصاً صالحاً، ولا نعيد الترحيل بعد تسجيله مرة واحدة.
        usdt_units_fixed = await self.session.get(ExchangeSetting, "usdt_rate_units_v2")
        if usdt_units_fixed is None:
            usdt_setting = await self.session.get(ExchangeSetting, "usdt_per_usd")
            usdt_value = self.settings.default_usdt_per_usd
            legacy_syp_value = False
            if usdt_setting is not None:
                try:
                    existing_usdt_value = Decimal(usdt_setting.value)
                except InvalidOperation:
                    existing_usdt_value = Decimal("0")
                legacy_syp_value = (
                    existing_usdt_value == self.settings.default_syp_per_usd
                    and self.settings.default_syp_per_usd != self.settings.default_usdt_per_usd
                )
                if existing_usdt_value > 0 and not legacy_syp_value:
                    usdt_value = existing_usdt_value
            if usdt_setting is None:
                self.session.add(ExchangeSetting(key="usdt_per_usd", value=str(usdt_value)))
            elif legacy_syp_value or usdt_setting.value != str(usdt_value):
                usdt_setting.value = str(usdt_value)
            for code in (RouteCode.USD_TO_USDT, RouteCode.USDT_TO_USD):
                route = await self.get_route(code)
                if route.rate_value is None or route.rate_value <= 0 or (
                    legacy_syp_value and route.rate_value == self.settings.default_syp_per_usd
                ):
                    route.rate_value = usdt_value
            self.session.add(ExchangeSetting(key="usdt_rate_units_v2", value="1"))
        await self._backfill_legacy_wallet_credit_lots()
        await self.session.commit()

    async def _backfill_legacy_wallet_credit_lots(self) -> None:
        """يهيّئ FIFO للمحافظ الموجودة دون تعديل قيم الرصيد أو إسقاط قيودها."""
        wallets = list((await self.session.scalars(select(Wallet).order_by(Wallet.user_id))).all())
        for wallet in wallets:
            existing_lot = await self.session.scalar(
                select(WalletCreditLot.id).where(WalletCreditLot.user_id == wallet.user_id).limit(1)
            )
            if existing_lot is not None:
                continue
            if (
                Decimal(str(wallet.available_balance or 0)) == 0
                and Decimal(str(wallet.reserved_balance or 0)) == 0
            ):
                continue
            savepoint = await self.session.begin_nested()

            entries = list((await self.session.scalars(
                select(WalletLedgerEntry)
                .where(WalletLedgerEntry.user_id == wallet.user_id)
                .order_by(WalletLedgerEntry.created_at.asc(), WalletLedgerEntry.id.asc())
            )).all())
            ledger_total = sum((Decimal(str(entry.amount or 0)) for entry in entries), Decimal("0"))
            available = Decimal(str(wallet.available_balance or 0))
            adjustment = available - ledger_total
            lots: list[WalletCreditLot] = []

            for entry in entries:
                amount = Decimal(str(entry.amount or 0))
                if amount <= 0 or entry.entry_type == "WITHDRAWAL_RELEASE":
                    continue
                lot = WalletCreditLot(
                    user_id=wallet.user_id,
                    ledger_entry_id=entry.id,
                    original_amount=amount,
                    remaining_amount=amount,
                    created_at=entry.created_at,
                )
                self.session.add(lot)
                lots.append(lot)

            if adjustment != 0:
                adjustment_key = f"wallet-credit-lot-backfill:{wallet.user_id}:v1"
                adjustment_entry = WalletLedgerEntry(
                    user_id=wallet.user_id,
                    amount=adjustment,
                    entry_type="LEGACY_WALLET_RECONCILIATION",
                    idempotency_key=adjustment_key,
                    details="Backfilled FIFO source tracking; wallet balance was not changed.",
                    created_at=wallet.created_at,
                )
                self.session.add(adjustment_entry)
                await self.session.flush()
                if adjustment > 0:
                    lot = WalletCreditLot(
                        user_id=wallet.user_id,
                        ledger_entry_id=adjustment_entry.id,
                        original_amount=adjustment,
                        remaining_amount=adjustment,
                        created_at=wallet.created_at,
                    )
                    self.session.add(lot)
                    lots.append(lot)

            await self.session.flush()
            lots.sort(key=lambda lot: (lot.created_at, lot.id or 0))
            requests = list((await self.session.scalars(
                select(WithdrawalRequest)
                .where(
                    WithdrawalRequest.user_id == wallet.user_id,
                    WithdrawalRequest.status.in_((WithdrawalStatus.PENDING_REVIEW, WithdrawalStatus.PAID)),
                )
                .order_by(WithdrawalRequest.created_at.asc(), WithdrawalRequest.id.asc())
            )).all())

            allocation_failed = False
            for request in requests:
                allocations_exist = await self.session.scalar(
                    select(WalletWithdrawalAllocation.id)
                    .where(WalletWithdrawalAllocation.withdrawal_request_id == request.id)
                    .limit(1)
                )
                if allocations_exist is not None:
                    allocation_failed = True
                    break
                left = Decimal(str(request.amount or 0))
                plan: list[tuple[WalletCreditLot, Decimal]] = []
                for lot in lots:
                    if lot.created_at and request.created_at and lot.created_at > request.created_at:
                        continue
                    remaining = Decimal(str(lot.remaining_amount or 0))
                    take = min(left, remaining)
                    if take > 0:
                        plan.append((lot, take))
                        left -= take
                    if left == 0:
                        break
                if left != 0:
                    allocation_failed = True
                    break
                for lot, amount in plan:
                    lot.remaining_amount = Decimal(str(lot.remaining_amount or 0)) - amount
                    self.session.add(WalletWithdrawalAllocation(
                        withdrawal_request_id=request.id,
                        credit_lot_id=lot.id,
                        amount=amount,
                    ))

            if allocation_failed:
                # Leave the exact legacy balance untouched; withdrawals and reversals
                # fail closed until its historical ledger can be reconciled manually.
                await savepoint.rollback()
                continue

            if adjustment < 0:
                left = -adjustment
                for lot in lots:
                    remaining = Decimal(str(lot.remaining_amount or 0))
                    take = min(left, remaining)
                    if take > 0:
                        lot.remaining_amount = remaining - take
                        left -= take
                    if left == 0:
                        break
                if left != 0:
                    await savepoint.rollback()
                    continue

            await self.session.flush()
            await savepoint.commit()

    async def _settings(self) -> dict[str, str]:
        rows = (await self.session.scalars(select(ExchangeSetting))).all()
        return {row.key: row.value for row in rows}

    async def current_rates(self) -> ExchangeRates:
        values = await self._settings()
        try:
            rates = ExchangeRates(
                syp_per_usd=Decimal(values["syp_per_usd"]),
                usdt_per_usd=Decimal(values["usdt_per_usd"]),
                minimum_usd=self.settings.minimum_usd,
                completion_eta=self.settings.completion_eta,
            )
        except (KeyError, InvalidOperation) as error:
            raise RuntimeError("إعدادات الأسعار غير مكتملة أو غير صالحة.") from error
        if rates.syp_per_usd <= 0 or rates.usdt_per_usd <= 0 or rates.minimum_usd <= 0:
            raise RuntimeError("إعدادات الأسعار يجب أن تكون موجبة.")
        return rates

    async def all_routes(self) -> list[ExchangeRoute]:
        routes = list((await self.session.scalars(select(ExchangeRoute))).all())
        return sorted(routes, key=lambda item: list(RouteCode).index(item.code))

    async def enabled_routes(self) -> list[ExchangeRoute]:
        routes = await self.all_routes()
        return [route for route in routes if route.is_enabled]

    async def get_route(self, route_code: RouteCode) -> ExchangeRoute:
        route = await self.session.get(ExchangeRoute, route_code)
        if route is None:
            raise DomainError("مسار التحويل غير موجود.")
        return route

    async def update_rate(self, key: str, raw_value: str, actor_id: int) -> Decimal:
        if key == "minimum_usd":
            raise DomainError("الحد الأدنى موحّد ويُدار من MINIMUM_USD في ملف البيئة.")
        if key not in {"syp_per_usd", "usdt_per_usd"}:
            raise DomainError("إعداد السعر غير صالح.")
        value = decimal_from_text(raw_value)
        await self._upsert_setting(key, str(value), actor_id)
        self.session.add(AuditLog(actor_id=actor_id, action=AuditAction.RATE_UPDATED, details=f"{key}={value}"))
        await self.session.commit()
        return value

    async def record_user(self, user_id: int, chat_id: int, username: str | None, full_name: str) -> bool:
        user = await self.session.get(BotUser, user_id)
        now = utc_now()
        if user is None:
            self.session.add(BotUser(user_id=user_id, chat_id=chat_id, username=(username or "")[:64] or None,
                                     full_name=full_name[:256], first_seen_at=now, last_active_at=now))
            await self.session.commit()
            return False
        normalized_username = (username or "")[:64] or None
        normalized_full_name = full_name[:256]
        profile_changed = (
            user.chat_id != chat_id
            or user.username != normalized_username
            or user.full_name != normalized_full_name
        )
        last_active_at = user.last_active_at
        if last_active_at is not None and last_active_at.tzinfo is None:
            last_active_at = last_active_at.replace(tzinfo=UTC)
        activity_write_due = (
            last_active_at is None
            or now - last_active_at >= _USER_ACTIVITY_WRITE_INTERVAL
        )
        if profile_changed:
            user.chat_id = chat_id
            user.username = normalized_username
            user.full_name = normalized_full_name
        if activity_write_due:
            user.last_active_at = now
        if profile_changed or activity_write_due:
            await self.session.commit()
        return user.is_banned

    async def _credit_wallet(
        self,
        user_id: int,
        amount: Decimal,
        entry_type: str,
        idempotency_key: str,
        details: str | None = None,
    ) -> WalletLedgerEntry | None:
        """يسجل credit مع lot مستقل؛ القيد الموجود مسبقاً لا يُكرر."""
        existing = await self.session.scalar(
            select(WalletLedgerEntry).where(WalletLedgerEntry.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing
        wallet = await self.session.scalar(select(Wallet).where(Wallet.user_id == user_id).with_for_update())
        if wallet is None:
            wallet = Wallet(user_id=user_id)
            self.session.add(wallet)
            await self.session.flush()
        amount = Decimal(str(amount))
        if amount < 0:
            raise DomainError("قيمة credit غير صالحة.")
        wallet.available_balance = (wallet.available_balance or Decimal("0")) + amount
        entry = WalletLedgerEntry(
            user_id=user_id,
            amount=amount,
            entry_type=entry_type,
            idempotency_key=idempotency_key,
            details=details,
        )
        self.session.add(entry)
        await self.session.flush()
        if amount > 0:
            self.session.add(
                WalletCreditLot(
                    user_id=user_id,
                    ledger_entry_id=entry.id,
                    original_amount=amount,
                    remaining_amount=amount,
                )
            )
        return entry

    async def register_activation(self, user_id: int, referral_code: str | None = None) -> WalletSummary:
        """يثبت تفعيل المستخدم، ويسجل إحالة البرنامج الجديد مرة واحدة."""
        async with _REFERRAL_LOCK:
            return await self._register_activation(user_id, referral_code)

    async def _register_activation(self, user_id: int, referral_code: str | None = None) -> WalletSummary:
        profile = await self.session.scalar(
            select(ReferralProfile).where(ReferralProfile.user_id == user_id).with_for_update()
        )
        if profile is None:
            profile = ReferralProfile(user_id=user_id, referral_code=await self._new_referral_code())
            self.session.add(profile)
            await self.session.flush()

        await self._credit_wallet(
            user_id,
            self.settings.referral_signup_bonus,
            "SIGNUP_BONUS",
            f"signup:{user_id}",
        )

        normalized_code = referral_code.strip().upper() if referral_code else None
        if normalized_code and profile.referrer_user_id is None:
            referrer = await self.session.scalar(
                select(ReferralProfile)
                .where(ReferralProfile.referral_code == normalized_code)
                .with_for_update()
            )
            if (
                referrer
                and referrer.user_id != user_id
                and not await self._referral_would_cycle(referrer.user_id, user_id)
            ):
                existing_event = await self.session.scalar(
                    select(ReferralEvent)
                    .where(ReferralEvent.referred_user_id == user_id)
                    .with_for_update()
                )
                # An event (including a revoked one) is permanently unique and cannot re-count.
                profile.referrer_user_id = existing_event.referrer_user_id if existing_event else referrer.user_id
                if existing_event is None:
                    active_count = await self._active_level_count(referrer.user_id)
                    level = referral_level_for_count(active_count + 1)
                    reward = referral_reward_for_level(self.settings, level)
                    referrer.referral_count = max(0, int(referrer.referral_count or 0)) + 1
                    event = ReferralEvent(
                        referrer_user_id=referrer.user_id,
                        referred_user_id=user_id,
                        is_active=True,
                        eligible_for_levels=True,
                        reward_granted=reward,
                        reward_reversed=Decimal("0"),
                    )
                    self.session.add(event)
                    await self.session.flush()
                    if reward > 0:
                        await self._credit_wallet(
                            referrer.user_id,
                            reward,
                            "REFERRAL_REWARD",
                            f"referral:{referrer.user_id}:{user_id}",
                            details=f"level={level};event={event.id}",
                        )

        await self.session.commit()
        return await self.wallet_summary(user_id)

    async def _active_level_count(self, referrer_user_id: int) -> int:
        count = await self.session.scalar(
            select(func.count(ReferralEvent.id)).where(
                ReferralEvent.referrer_user_id == referrer_user_id,
                ReferralEvent.is_active.is_(True),
                ReferralEvent.eligible_for_levels.is_(True),
            )
        )
        return int(count or 0)

    async def _referral_would_cycle(self, referrer_user_id: int, user_id: int) -> bool:
        """يتحقق من أن ربط المستخدم لا ينشئ دورة في سلسلة المحيلين."""
        current_user_id: int | None = referrer_user_id
        visited: set[int] = set()
        while current_user_id is not None and current_user_id not in visited:
            if current_user_id == user_id:
                return True
            visited.add(current_user_id)
            current_user_id = await self.session.scalar(
                select(ReferralProfile.referrer_user_id).where(
                    ReferralProfile.user_id == current_user_id
                )
            )
        return False

    def _referral_reward(self, referral_count: int) -> Decimal:
        """توافق داخلي: reward يحدده المستوى الثابت لا الإعدادات القديمة."""
        return referral_reward_for_level(self.settings, referral_level_for_count(referral_count))

    async def wallet_summary(self, user_id: int) -> WalletSummary:
        profile = await self.session.get(ReferralProfile, user_id)
        if profile is None:
            profile = ReferralProfile(user_id=user_id, referral_code=await self._new_referral_code())
            self.session.add(profile)
            await self.session.flush()
        wallet = await self.session.get(Wallet, user_id)
        if wallet is None:
            wallet = Wallet(user_id=user_id)
            self.session.add(wallet)
            await self.session.commit()
        referral_count = await self._active_level_count(user_id)
        level = referral_level_for_count(referral_count)
        account = await self.session.scalar(
            select(WithdrawalAccount)
            .where(WithdrawalAccount.user_id == user_id, WithdrawalAccount.is_active.is_(True))
            .order_by(WithdrawalAccount.id.desc())
        )
        return WalletSummary(
            balance=wallet.available_balance or Decimal("0"),
            reserved=wallet.reserved_balance or Decimal("0"),
            referrals=referral_count,
            referral_code=profile.referral_code,
            account_method=account.method if account else None,
            account_value=account.account_value if account else None,
            withdrawal_minimum=self.settings.withdrawal_minimum_usd,
            referral_level=level,
            referral_reward=referral_reward_for_level(self.settings, level),
        )

    async def revoke_referral_for_referred_user(self, referred_user_id: int, reason: str) -> bool:
        """يعطل الإحالة مرة واحدة ويعكس فقط الجزء المتاح من reward عبر FIFO."""
        async with _REFERRAL_LOCK:
            event = await self.session.scalar(
                select(ReferralEvent)
                .where(ReferralEvent.referred_user_id == referred_user_id)
                .with_for_update()
            )
            if event is None:
                return False
            if event.is_active:
                event.is_active = False
                event.revoked_at = utc_now()
                event.revocation_reason = clean_short_text(reason, "سبب إلغاء الإحالة", maximum_length=512)
                profile = await self.session.scalar(
                    select(ReferralProfile)
                    .where(ReferralProfile.user_id == event.referrer_user_id)
                    .with_for_update()
                )
                if profile is not None:
                    profile.referral_count = max(0, int(profile.referral_count or 0) - 1)
            elif not event.revocation_reason and reason:
                event.revocation_reason = clean_short_text(reason, "سبب إلغاء الإحالة", maximum_length=512)

            wallet = await self.session.scalar(
                select(Wallet).where(Wallet.user_id == event.referrer_user_id).with_for_update()
            )
            if wallet is not None:
                await self._reverse_available_revoked_referral_rewards(event.referrer_user_id, wallet)
            await self.session.commit()
            return True

    async def _reverse_available_revoked_referral_rewards(self, referrer_user_id: int, wallet: Wallet) -> None:
        """يعكس حصراً رصيد مكافأة الإحالة الذي ما زال متاحاً في lot نفسه."""
        await self.session.flush()
        if not await self._wallet_integrity_is_valid(wallet):
            return
        events = list((await self.session.scalars(
            select(ReferralEvent)
            .where(
                ReferralEvent.referrer_user_id == referrer_user_id,
                ReferralEvent.is_active.is_(False),
                ReferralEvent.reward_granted > ReferralEvent.reward_reversed,
            )
            .order_by(ReferralEvent.id.asc())
            .with_for_update()
        )).all())
        for event in events:
            remaining_reward = max(
                Decimal("0"),
                Decimal(str(event.reward_granted or 0)) - Decimal(str(event.reward_reversed or 0)),
            )
            if remaining_reward <= 0:
                continue
            source = await self.session.scalar(
                select(WalletLedgerEntry).where(
                    WalletLedgerEntry.idempotency_key
                    == f"referral:{event.referrer_user_id}:{event.referred_user_id}"
                )
            )
            if source is None or source.entry_type != "REFERRAL_REWARD":
                continue
            lot = await self.session.scalar(
                select(WalletCreditLot)
                .where(WalletCreditLot.ledger_entry_id == source.id)
                .with_for_update()
            )
            if (
                lot is None
                or lot.user_id != referrer_user_id
                or Decimal(str(source.amount or 0)) != Decimal(str(event.reward_granted or 0))
                or Decimal(str(lot.original_amount or 0)) != Decimal(str(source.amount or 0))
            ):
                continue
            available = Decimal(str(lot.remaining_amount or 0))
            amount = min(remaining_reward, available, Decimal(str(wallet.available_balance or 0)))
            if amount <= 0:
                continue
            key = f"referral-reversal:{event.id}:{event.reward_reversed}"
            if await self.session.scalar(
                select(WalletLedgerEntry.id).where(WalletLedgerEntry.idempotency_key == key)
            ) is not None:
                continue
            lot.remaining_amount = available - amount
            wallet.available_balance = Decimal(str(wallet.available_balance or 0)) - amount
            event.reward_reversed = Decimal(str(event.reward_reversed or 0)) + amount
            self.session.add(WalletLedgerEntry(
                user_id=referrer_user_id,
                amount=-amount,
                entry_type="REFERRAL_REWARD_REVERSAL",
                idempotency_key=key,
                details=f"event={event.id};reason={event.revocation_reason}",
            ))
            await self.session.flush()

    async def _wallet_integrity_is_valid(self, wallet: Wallet) -> bool:
        available = Decimal(str(wallet.available_balance or 0))
        ledger_total = await self.session.scalar(
            select(func.coalesce(func.sum(WalletLedgerEntry.amount), 0)).where(
                WalletLedgerEntry.user_id == wallet.user_id
            )
        )
        lots_total = await self.session.scalar(
            select(func.coalesce(func.sum(WalletCreditLot.remaining_amount), 0)).where(
                WalletCreditLot.user_id == wallet.user_id
            )
        )
        pending_reserved = await self.session.scalar(
            select(func.coalesce(func.sum(WalletWithdrawalAllocation.amount), 0))
            .join(WithdrawalRequest, WithdrawalRequest.id == WalletWithdrawalAllocation.withdrawal_request_id)
            .where(
                WithdrawalRequest.user_id == wallet.user_id,
                WithdrawalRequest.status == WithdrawalStatus.PENDING_REVIEW,
            )
        )
        return (
            available == Decimal(str(ledger_total or 0))
            and available == Decimal(str(lots_total or 0))
            and Decimal(str(wallet.reserved_balance or 0)) == Decimal(str(pending_reserved or 0))
            and available >= 0
            and Decimal(str(wallet.reserved_balance or 0)) >= 0
        )

    async def referral_admin_leaderboard(self, limit: int = 50) -> list[LeaderboardEntry]:
        """قائمة إدارة داخلية لإحالات البرنامج الجديد النشطة فقط."""
        active_count = func.count(ReferralEvent.id)
        rows = (await self.session.execute(
            select(ReferralEvent.referrer_user_id, active_count, BotUser.full_name)
            .join(BotUser, BotUser.user_id == ReferralEvent.referrer_user_id, isouter=True)
            .where(
                ReferralEvent.is_active.is_(True),
                ReferralEvent.eligible_for_levels.is_(True),
            )
            .group_by(ReferralEvent.referrer_user_id, BotUser.full_name)
            .order_by(active_count.desc(), ReferralEvent.referrer_user_id.asc())
            .limit(limit)
        )).all()
        return [
            LeaderboardEntry(user_id=row[0], display_name=row[2] or f"مستخدم {row[0]}", referrals=int(row[1]))
            for row in rows
        ]

    async def save_withdrawal_account(self, user_id: int, method: str, account_value: str) -> WithdrawalAccount:
        method = clean_short_text(method, "طريقة السحب", maximum_length=32)
        account_value = clean_short_text(account_value, "حساب السحب", maximum_length=256)
        await self.session.execute(
            WithdrawalAccount.__table__.update()
            .where(WithdrawalAccount.user_id == user_id)
            .values(is_active=False)
        )
        account = WithdrawalAccount(user_id=user_id, method=method, account_value=account_value)
        self.session.add(account)
        await self.session.commit()
        return account

    async def create_withdrawal(self, user_id: int, amount_text: str) -> WithdrawalRequest:
        raw_amount = decimal_from_text(amount_text)
        amount = raw_amount.quantize(USD_QUANTUM, rounding=ROUND_DOWN)
        if amount != raw_amount:
            raise DomainError("مبلغ السحب لا يقبل أكثر من منزلتين عشريتين.")

        if amount < self.settings.withdrawal_minimum_usd:
            raise DomainError(f"الحد الأدنى للسحب هو {format_amount(self.settings.withdrawal_minimum_usd, Currency.USD)}.")
        wallet = await self.session.scalar(select(Wallet).where(Wallet.user_id == user_id).with_for_update())
        account = await self.session.scalar(
            select(WithdrawalAccount)
            .where(WithdrawalAccount.user_id == user_id, WithdrawalAccount.is_active.is_(True))
            .order_by(WithdrawalAccount.id.desc())
        )
        if wallet is None or account is None:
            raise DomainError("أضف حساب السحب أولاً ثم أعد المحاولة.")
        if not await self._wallet_integrity_is_valid(wallet):
            raise DomainError("تعذر التحقق من دفتر المحفظة؛ لم يُخصم أي رصيد.")

        lots = list((await self.session.scalars(
            select(WalletCreditLot)
            .where(WalletCreditLot.user_id == user_id, WalletCreditLot.remaining_amount > 0)
            .order_by(WalletCreditLot.created_at.asc(), WalletCreditLot.id.asc())
            .with_for_update()
        )).all())
        if sum((Decimal(str(lot.remaining_amount or 0)) for lot in lots), Decimal("0")) < amount:
            raise DomainError("الرصيد المتاح غير كافٍ لإتمام طلب السحب.")

        request = WithdrawalRequest(
            public_id=await self._new_withdrawal_id(),
            user_id=user_id,
            account_id=account.id,
            method_snapshot=account.method,
            account_snapshot=account.account_value,
            amount=amount,
            review_not_before=utc_now() + timedelta(hours=self.settings.withdrawal_review_hours),
        )
        self.session.add(request)
        await self.session.flush()
        left = amount
        for lot in lots:
            if left <= 0:
                break
            lot_available = Decimal(str(lot.remaining_amount or 0))
            allocation_amount = min(left, lot_available)
            if allocation_amount <= 0:
                continue
            lot.remaining_amount = lot_available - allocation_amount
            self.session.add(WalletWithdrawalAllocation(
                withdrawal_request_id=request.id,
                credit_lot_id=lot.id,
                amount=allocation_amount,
            ))
            left -= allocation_amount
        if left != 0:
            raise DomainError("تعذر تخصيص السحب محاسبياً؛ لم يُخصم أي رصيد.")
        wallet.available_balance = (wallet.available_balance or Decimal("0")) - amount
        wallet.reserved_balance = (wallet.reserved_balance or Decimal("0")) + amount
        self.session.add(WalletLedgerEntry(
            user_id=user_id,
            amount=-amount,
            entry_type="WITHDRAWAL_HOLD",
            idempotency_key=f"withdrawal-hold:{request.public_id}",
            details="FIFO credit-lot allocation",
        ))
        self.session.add(AuditLog(
            actor_id=user_id,
            action=AuditAction.WITHDRAWAL_HOLD,
            request_public_id=request.public_id,
            details=f"amount={amount}",
        ))
        await self.session.commit()
        return request

    async def pending_withdrawals(self, limit: int = 50) -> list[WithdrawalRequest]:
        statement = select(WithdrawalRequest).where(
            WithdrawalRequest.status == WithdrawalStatus.PENDING_REVIEW
        ).order_by(WithdrawalRequest.created_at.asc()).limit(limit)
        return list((await self.session.scalars(statement)).all())

    async def resolve_withdrawal(
        self,
        public_id: str,
        actor_id: int,
        approved: bool,
        note: str | None = None,
    ) -> WithdrawalRequest:
        if not approved:
            async with _REFERRAL_LOCK:
                return await self._resolve_withdrawal_transaction(
                    public_id, actor_id, approved, note
                )
        return await self._resolve_withdrawal_transaction(
            public_id, actor_id, approved, note
        )
    async def _resolve_withdrawal_transaction(
        self,
        public_id: str,
        actor_id: int,
        approved: bool,
        note: str | None = None,
    ) -> WithdrawalRequest:
        request = await self.session.scalar(
            select(WithdrawalRequest).where(WithdrawalRequest.public_id == public_id).with_for_update()
        )
        if request is None or request.status is not WithdrawalStatus.PENDING_REVIEW:
            raise DomainError("طلب السحب غير متاح للمراجعة.")
        if approved and ensure_utc(request.review_not_before) > utc_now():
            raise DomainError("لم تنتهِ فترة المراجعة الأمنية لهذا الطلب بعد.")
        wallet = await self.session.scalar(select(Wallet).where(Wallet.user_id == request.user_id).with_for_update())
        if wallet is None or not await self._wallet_integrity_is_valid(wallet):
            raise DomainError("تعذر التحقق من دفتر المحفظة؛ لم يتغير طلب السحب.")
        allocations = list((await self.session.scalars(
            select(WalletWithdrawalAllocation)
            .where(WalletWithdrawalAllocation.withdrawal_request_id == request.id)
            .with_for_update()
        )).all())
        allocated = sum((Decimal(str(item.amount or 0)) for item in allocations), Decimal("0"))
        if allocated != Decimal(str(request.amount)):
            raise DomainError("تخصيص طلب السحب غير متطابق؛ لم يتغير الطلب.")

        amount = Decimal(str(request.amount))
        wallet.reserved_balance = (wallet.reserved_balance or Decimal("0")) - amount
        if wallet.reserved_balance < 0:
            raise DomainError("رصيد الحجز غير متطابق؛ لم يتغير الطلب.")
        request.status = WithdrawalStatus.PAID if approved else WithdrawalStatus.REJECTED
        if not approved:
            for allocation in allocations:
                lot = await self.session.get(WalletCreditLot, allocation.credit_lot_id, with_for_update=True)
                if lot is None:
                    raise DomainError("credit lot مفقود؛ لم يتغير الطلب.")
                lot.remaining_amount = (lot.remaining_amount or Decimal("0")) + allocation.amount
            wallet.available_balance = (wallet.available_balance or Decimal("0")) + amount
            self.session.add(WalletLedgerEntry(
                user_id=request.user_id,
                amount=amount,
                entry_type="WITHDRAWAL_RELEASE",
                idempotency_key=f"withdrawal-release:{request.public_id}",
                details="restored FIFO credit-lot allocation",
            ))
        else:
            # القيد الصفري يثبت إتمام الدفع؛ credit lots استُهلكت عند الحجز.
            self.session.add(WalletLedgerEntry(
                user_id=request.user_id,
                amount=Decimal("0"),
                entry_type="WITHDRAWAL_PAID",
                idempotency_key=f"withdrawal-paid:{request.public_id}",
            ))
        request.reviewed_by = actor_id
        request.reviewed_at = utc_now()
        request.decision_note = clean_short_text(note, "ملاحظة القرار", maximum_length=512) if note else None
        self.session.add(AuditLog(
            actor_id=actor_id,
            action=AuditAction.WITHDRAWAL_PAID if approved else AuditAction.WITHDRAWAL_RELEASED,
            request_public_id=request.public_id,
            details=request.decision_note,
        ))
        # رد السحب وعكس المكافآت الملغاة يجب أن ينجزا قبل الحفظ نفسه؛
        # إبقاء الرصيد المستعاد قابلاً للسحب بين معاملتين يسمح بتجاوزه.
        if not approved:
            await self._reverse_available_revoked_referral_rewards(
                request.user_id,
                wallet,
            )

        await self.session.commit()

        return request

    async def is_banned(self, user_id: int) -> bool:
        user = await self.session.get(BotUser, user_id)
        return bool(user and user.is_banned)

    async def set_user_banned(self, user_id: int, banned: bool, actor_id: int | None = None) -> None:
        user = await self.session.get(BotUser, user_id)
        if user is None:
            if not banned:
                raise DomainError("لم يُسجّل هذا المستخدم في البوت.")
            now = utc_now()
            user = BotUser(user_id=user_id, chat_id=user_id, full_name="مستخدم محظور",
                           is_banned=True, first_seen_at=now, last_active_at=now)
            self.session.add(user)
            if actor_id is not None:
                self.session.add(AuditLog(
                    actor_id=actor_id,
                    action=AuditAction.USER_BANNED,
                    details=f"user_id={user_id}",
                ))
            await self.session.commit()
            return
        user.is_banned = banned
        if actor_id is not None:
            self.session.add(AuditLog(
                actor_id=actor_id,
                action=AuditAction.USER_BANNED if banned else AuditAction.USER_UNBANNED,
                details=f"user_id={user_id}",
            ))
        await self.session.commit()

    async def user_chat_ids(self) -> list[int]:
        rows = await self.session.scalars(select(BotUser.chat_id).where(BotUser.is_banned.is_(False)))
        return list(rows.all())

    async def monitoring_enabled(self) -> bool:
        setting = await self.session.get(ExchangeSetting, "monitoring_enabled")
        return bool(setting and setting.value == "1")

    async def set_monitoring(self, enabled: bool, actor_id: int) -> None:
        await self._upsert_setting("monitoring_enabled", "1" if enabled else "0", actor_id)
        await self.session.commit()

    async def get_setting_value(self, key: str, default: str = "") -> str:
        setting = await self.session.get(ExchangeSetting, key)
        return setting.value if setting else default

    async def set_setting_value(self, key: str, value: str, actor_id: int) -> None:
        await self._upsert_setting(key, value, actor_id)
        await self.session.commit()

    async def update_route_address(self, route_code: RouteCode, raw_value: str, actor_id: int) -> str:
        route = await self.get_route(route_code)
        value = clean_short_text(raw_value, "عنوان أو كود الدفع")
        route.payment_address = value
        route.updated_by = actor_id
        self.session.add(
            AuditLog(actor_id=actor_id, action=AuditAction.ROUTE_UPDATED, details=f"address:{route_code.value}")
        )
        await self.session.commit()
        return value

    async def update_route_barcode(self, route_code: RouteCode, file_id: str, actor_id: int) -> None:
        route = await self.get_route(route_code)
        if not file_id or len(file_id) > 256:
            raise DomainError("صورة الباركود غير صالحة.")
        route.barcode_file_id = file_id
        route.updated_by = actor_id
        self.session.add(
            AuditLog(actor_id=actor_id, action=AuditAction.ROUTE_UPDATED, details=f"barcode:{route_code.value}")
        )
        await self.session.commit()

    async def crypto_method_values(
        self,
        method: str,
    ) -> tuple[str | None, str | None]:
        allowed_methods = {
            "TRC20",
            "ERC20",
            "BEP20",
            "TON",
            "CWALLET",
            "BINANCE",
        }
        if method not in allowed_methods:
            raise DomainError("طريقة USDT غير صالحة.")

        address_setting = await self.session.get(
            ExchangeSetting,
            f"crypto_method:{method}:address",
        )
        barcode_setting = await self.session.get(
            ExchangeSetting,
            f"crypto_method:{method}:barcode",
        )

        return (
            address_setting.value if address_setting else None,
            barcode_setting.value if barcode_setting else None,
        )

    async def update_crypto_method_address(
        self,
        method: str,
        raw_value: str,
        actor_id: int,
    ) -> str:
        allowed_methods = {
            "TRC20",
            "ERC20",
            "BEP20",
            "TON",
            "CWALLET",
            "BINANCE",
        }
        if method not in allowed_methods:
            raise DomainError("طريقة USDT غير صالحة.")

        value = clean_short_text(raw_value, "عنوان المحفظة أو الحساب")
        await self._upsert_setting(
            f"crypto_method:{method}:address",
            value,
            actor_id,
        )
        self.session.add(
            AuditLog(
                actor_id=actor_id,
                action=AuditAction.ROUTE_UPDATED,
                details=f"crypto_method_address:{method}",
            )
        )
        await self.session.commit()
        return value

    async def update_crypto_method_barcode(
        self,
        method: str,
        file_id: str,
        actor_id: int,
    ) -> None:
        allowed_methods = {
            "TRC20",
            "ERC20",
            "BEP20",
            "TON",
            "CWALLET",
            "BINANCE",
        }
        if method not in allowed_methods:
            raise DomainError("طريقة USDT غير صالحة.")
        if not file_id or len(file_id) > 256:
            raise DomainError("صورة QR غير صالحة.")

        await self._upsert_setting(
            f"crypto_method:{method}:barcode",
            file_id,
            actor_id,
        )
        self.session.add(
            AuditLog(
                actor_id=actor_id,
                action=AuditAction.ROUTE_UPDATED,
                details=f"crypto_method_barcode:{method}",
            )
        )
        await self.session.commit()


    async def set_route_enabled(self, route_code: RouteCode, enabled: bool, actor_id: int) -> None:
        route = await self.get_route(route_code)
        route.is_enabled = enabled
        route.updated_by = actor_id
        self.session.add(
            AuditLog(
                actor_id=actor_id,
                action=AuditAction.ROUTE_UPDATED,
                details=f"enabled:{route_code.value}={enabled}",
            )
        )
        await self.session.commit()

    async def create_draft(
        self,
        *,
        route_code: RouteCode,
        user_id: int,
        chat_id: int,
        username: str | None,
        full_name: str,
    ) -> ExchangeRequest:
        route = await self.get_route(route_code)
        if not route.is_enabled:
            raise DomainError("هذا المسار متوقف مؤقتاً.")
        if not route.payment_address or not route.barcode_file_id:
            raise DomainError("هذا المسار غير مهيأ حالياً. تواصل مع الإدارة.")
        live_rate = await live_rate_for_route(route.code)
        rates = await self.current_rates()
        request = ExchangeRequest(
            public_id=await self._new_public_id(),
            route_code=route.code,
            user_id=user_id,
            chat_id=chat_id,
            username=username[:64] if username else None,
            full_name=full_name[:256],
            from_currency=route.from_currency,
            to_currency=route.to_currency,
            payment_address_snapshot=route.payment_address,
            barcode_file_id_snapshot=route.barcode_file_id,
            syp_per_usd_snapshot=rates.syp_per_usd,
            usdt_per_usd_snapshot=Decimal("1"),
            minimum_usd_snapshot=rates.minimum_usd,
            rate_snapshot=live_rate,
            fee_percentage_snapshot=self.settings.global_fee_percentage,
            eta_snapshot=rates.completion_eta,
            expires_at=utc_now() + timedelta(minutes=self.settings.quote_ttl_minutes),
        )
        self.session.add(request)
        self.session.add(AuditLog(actor_id=user_id, action=AuditAction.REQUEST_CREATED, request_public_id=request.public_id))
        await self.session.commit()
        return request

    async def create_target_draft(
        self,
        *,
        route_code: RouteCode,
        user_id: int,
        chat_id: int,
        username: str | None,
        full_name: str,
        raw_target_amount: str,
    ) -> ExchangeRequest:
        route = await self.get_route(route_code)
        if not route.is_enabled:
            raise DomainError("هذا الطلب متوقف مؤقتاً.")
        if route.from_currency is not Currency.USDT and (
            not route.payment_address or not route.barcode_file_id
        ):
            raise DomainError("هذا الطلب غير مهيأ حالياً. تواصل مع الإدارة.")

        live_rate = await live_rate_for_route(route.code)

        rates = await self.current_rates()
        target_amount = decimal_from_text(raw_target_amount)
        
        payment_amount, _, _, actual_net_amount = calculate_target_breakdown(
            target_amount=target_amount,
            route_code=route.code,
            rate=live_rate,
            fee_percentage=self.settings.global_fee_percentage,
            minimum_usd=rates.minimum_usd,
            usdt_per_usd=Decimal("1"),
        )


        request = ExchangeRequest(
            public_id=await self._new_public_id(),
            route_code=route.code,
            user_id=user_id,
            chat_id=chat_id,
            username=username[:64] if username else None,
            full_name=full_name[:256],
            from_currency=route.from_currency,
            to_currency=route.to_currency,
            payment_address_snapshot=route.payment_address or "",
            barcode_file_id_snapshot=(
                route.barcode_file_id
                if route.from_currency is not Currency.USDT
                else None
            ),
            from_amount=payment_amount,
            to_amount=actual_net_amount,
            syp_per_usd_snapshot=rates.syp_per_usd,
            usdt_per_usd_snapshot=Decimal("1"),
            minimum_usd_snapshot=rates.minimum_usd,
            rate_snapshot=live_rate,
            fee_percentage_snapshot=self.settings.global_fee_percentage,
            eta_snapshot=rates.completion_eta,
            status=RequestStatus.AWAITING_PAYOUT_ADDRESS,
            expires_at=utc_now()
            + timedelta(minutes=self.settings.review_ttl_minutes),
        )
        self.session.add(request)
        self.session.add(
            AuditLog(
                actor_id=user_id,
                action=AuditAction.REQUEST_CREATED,
                request_public_id=request.public_id,
            )
        )
        await self.session.commit()
        return request
    
    async def create_send_amount_draft(
        self,
        *,
        route_code: RouteCode,
        user_id: int,
        chat_id: int,
        username: str | None,
        full_name: str,
        raw_send_amount: str,
    ) -> ExchangeRequest:
        route = await self.get_route(route_code)
        if not route.is_enabled:
            raise DomainError("هذا الطلب متوقف مؤقتاً.")
        if route.from_currency is not Currency.USDT and (
            not route.payment_address or not route.barcode_file_id
        ):
            raise DomainError("هذا الطلب غير مهيأ حالياً. تواصل مع الإدارة.")

        live_rate = await live_rate_for_route(route.code)
        rates = await self.current_rates()

        send_amount = decimal_from_text(raw_send_amount)

        received_amount, _ = calculate_route_quote(
            amount=send_amount,
            route_code=route.code,
            rate=live_rate,
            fee_percentage=self.settings.global_fee_percentage,
            minimum_usd=rates.minimum_usd,
        )

        request = ExchangeRequest(
            public_id=await self._new_public_id(),
            route_code=route.code,
            user_id=user_id,
            chat_id=chat_id,
            username=username[:64] if username else None,
            full_name=full_name[:256],
            from_currency=route.from_currency,
            to_currency=route.to_currency,
            payment_address_snapshot=route.payment_address or "",
            barcode_file_id_snapshot=(
                route.barcode_file_id
                if route.from_currency is not Currency.USDT
                else None
            ),
            from_amount=send_amount,
            to_amount=received_amount,
            syp_per_usd_snapshot=rates.syp_per_usd,
            usdt_per_usd_snapshot=Decimal("1"),
            minimum_usd_snapshot=rates.minimum_usd,
            rate_snapshot=live_rate,
            fee_percentage_snapshot=self.settings.global_fee_percentage,
            eta_snapshot=rates.completion_eta,
            status=RequestStatus.AWAITING_PAYOUT_ADDRESS,
            expires_at=utc_now()
            + timedelta(minutes=self.settings.review_ttl_minutes),
        )
        self.session.add(request)
        self.session.add(
            AuditLog(
                actor_id=user_id,
                action=AuditAction.REQUEST_CREATED,
                request_public_id=request.public_id,
            )
        )
        await self.session.commit()
        return request

    async def prepare_user_review(
        self,
        public_id: str,
        user_id: int,
        raw_payout_address: str,
        crypto_method: str | None = None,
    ) -> ExchangeRequest:
        request = await self.get_request_for_user(public_id, user_id, lock=True)

        if request.status is not RequestStatus.AWAITING_PAYOUT_ADDRESS:
            raise DomainError("لا يمكن تجهيز مراجعة الطلب في هذه المرحلة.")

        # لا تسمح بإحياء عرض سعر انتهت صلاحيته قبل تمديد مهلة المراجعة.
        await self._ensure_not_expired(request)

        if request.from_amount is None or request.to_amount is None:
            raise DomainError("بيانات المبلغ غير مكتملة؛ ابدأ الطلب من جديد.")

        payout_address = clean_short_text(
            raw_payout_address,
            "عنوان الاستلام",
        )

        allowed_methods = {
            "TRC20",
            "ERC20",
            "BEP20",
            "TON",
            "CWALLET",
            "BINANCE",
        }

        if request.to_currency is Currency.USDT:
            if crypto_method not in allowed_methods:
                raise DomainError("اختر طريقة استلام USDT أولاً.")
            request.crypto_method = crypto_method

        elif request.from_currency is Currency.USDT:
            if crypto_method not in allowed_methods:
                raise DomainError("اختر طريقة إرسال USDT أولاً.")

            address_setting = await self.session.get(
                ExchangeSetting,
                f"crypto_method:{crypto_method}:address",
            )
            barcode_setting = await self.session.get(
                ExchangeSetting,
                f"crypto_method:{crypto_method}:barcode",
            )

            if (
                address_setting is None
                or not address_setting.value
                or barcode_setting is None
                or not barcode_setting.value
            ):
                raise DomainError(
                    "طريقة USDT المختارة غير مجهزة بعنوان الدفع وصورة QR. "
                    "اختر طريقة أخرى أو تواصل مع الإدارة."
                )

            request.crypto_method = crypto_method
            request.payment_address_snapshot = address_setting.value
            request.barcode_file_id_snapshot = barcode_setting.value

        else:
            request.crypto_method = None

        request.payout_address = payout_address
        request.status = RequestStatus.AWAITING_USER_REVIEW
        request.expires_at = utc_now() + timedelta(
            minutes=self.settings.review_ttl_minutes
        )

        await self.session.commit()
        return request

    async def set_user_review_message_id(
        self,
        public_id: str,
        user_id: int,
        message_id: int,
    ) -> None:
        if message_id <= 0:
            raise DomainError("رقم رسالة المراجعة غير صالح.")
        request = await self.get_request_for_user(public_id, user_id, lock=True)
        if request.status is not RequestStatus.AWAITING_USER_REVIEW:
            raise DomainError("لم يعد الطلب بانتظار مراجعة المستخدم.")
        request.review_message_id = message_id
        await self.session.commit()

    async def confirm_user_review(
        self,
        public_id: str,
        user_id: int,
    ) -> ExchangeRequest:
        request = await self.session.scalar(
            select(ExchangeRequest)
            .where(
                ExchangeRequest.public_id == public_id,
                ExchangeRequest.user_id == user_id,
            )
            .with_for_update()
        )
        if request is None:
            raise DomainError("لم يتم العثور على الطلب.")
        if request.status is not RequestStatus.AWAITING_USER_REVIEW:
            raise DomainError("لم يعد هذا الطلب بانتظار تأكيدك.")

        if ensure_utc(request.expires_at) <= utc_now():
            request.status = RequestStatus.EXPIRED
            self.session.add(
                AuditLog(
                    actor_id=user_id,
                    action=AuditAction.REQUEST_EXPIRED,
                    request_public_id=public_id,
                )
            )
            await self.session.commit()
            raise DomainError("انتهت صلاحية عرض السعر؛ ابدأ طلباً جديداً.")

        if (
            not request.payout_address
            or request.from_amount is None
            or request.to_amount is None
        ):
            raise DomainError("بيانات الطلب غير مكتملة؛ ابدأ طلباً جديداً.")

        request.status = RequestStatus.PENDING_PAYMENT
        request.review_message_id = None
        request.expires_at = utc_now() + timedelta(
            minutes=self.settings.quote_ttl_minutes
        )
        await self.session.commit()
        return request

    async def expire_user_review_if_due(
        self,
        public_id: str,
        user_id: int,
    ) -> bool:
        request = await self.session.scalar(
            select(ExchangeRequest)
            .where(
                ExchangeRequest.public_id == public_id,
                ExchangeRequest.user_id == user_id,
            )
            .with_for_update()
        )
        if request is None:
            return False
        if request.status not in {
            RequestStatus.AWAITING_USER_REVIEW,
            RequestStatus.EXPIRED,
        }:
            return False
        if ensure_utc(request.expires_at) > utc_now():
            return False

        if request.status is RequestStatus.AWAITING_USER_REVIEW:
            request.status = RequestStatus.EXPIRED
            self.session.add(
                AuditLog(
                    actor_id=user_id,
                    action=AuditAction.REQUEST_EXPIRED,
                    request_public_id=public_id,
                )
            )

        request.review_message_id = None
        await self.session.commit()
        return True

    async def activate_payment_expiry(
        self,
        public_id: str,
        user_id: int,
        message_id: int,
    ) -> datetime:
        request = await self.session.scalar(
            select(ExchangeRequest)
            .where(
                ExchangeRequest.public_id == public_id,
                ExchangeRequest.user_id == user_id,
            )
            .with_for_update()
        )
        if request is None:
            raise DomainError("لم يتم العثور على الطلب.")
        if request.status is not RequestStatus.PENDING_PAYMENT:
            raise DomainError("الطلب ليس في مرحلة انتظار الدفع.")
        if message_id <= 0:
            raise DomainError("رقم رسالة الدفع غير صالح.")

        if request.payment_message_id is not None:
            if request.payment_message_id == message_id:
                return request.expires_at
            raise DomainError("تم تسجيل رسالة الدفع لهذا الطلب مسبقاً.")

        request.payment_message_id = message_id
        request.expires_at = utc_now() + timedelta(
            minutes=self.settings.quote_ttl_minutes
        )
        self.session.add(
            AuditLog(
                actor_id=user_id,
                action=AuditAction.REQUEST_PAYMENT_STARTED,
                request_public_id=public_id,
            )
        )
        await self.session.commit()
        return request.expires_at

    async def expire_reference_if_due(
        self,
        public_id: str,
        user_id: int,
    ) -> bool:
        request = await self.session.scalar(
            select(ExchangeRequest)
            .where(
                ExchangeRequest.public_id == public_id,
                ExchangeRequest.user_id == user_id,
            )
            .with_for_update()
        )
        if request is None:
            return False

        if request.status not in {
            RequestStatus.AWAITING_REFERENCE,
            RequestStatus.PENDING_PAYMENT,
        }:
            return False

        if ensure_utc(request.expires_at) > utc_now():
            return False

        request.status = RequestStatus.EXPIRED
        self.session.add(
            AuditLog(
                actor_id=user_id,
                action=AuditAction.REQUEST_EXPIRED,
                request_public_id=public_id,
            )
        )
        await self.session.commit()
        return True
    
    async def set_payment_proof(
        self,
        public_id: str,
        user_id: int,
        file_id: str,
        file_type: str,
    ) -> ExchangeRequest:
        request = await self.session.scalar(
            select(ExchangeRequest)
            .where(
                ExchangeRequest.public_id == public_id,
                ExchangeRequest.user_id == user_id,
            )
            .with_for_update()
        )
        if request is None:
            raise DomainError("لم يتم العثور على الطلب.")

        if request.status is not RequestStatus.PENDING_PAYMENT:
            raise DomainError("لا يمكن إرسال إثبات الدفع في هذه المرحلة.")

        await self._ensure_not_expired(request)

        proof_type = file_type.strip().lower()
        proof_file_id = file_id.strip()
        if proof_type not in {"photo", "pdf"} or not proof_file_id or len(proof_file_id) > 256:
            raise DomainError("نوع ملف إثبات الدفع غير صالح.")

        request.payment_proof_file_id = proof_file_id
        request.payment_proof_type = proof_type
        request.payment_reported_at = utc_now()
        request.status = RequestStatus.PENDING_VERIFICATION

        self.session.add(
            AuditLog(
                actor_id=user_id,
                action=AuditAction.REQUEST_PAYMENT_REPORTED,
                request_public_id=public_id,
                details=f"proof:{proof_type}",
            )
        )
        await self.session.commit()
        return request
    
    async def set_payment_reference(
        self,
        public_id: str,
        user_id: int,
        raw_reference: str,
    ) -> ExchangeRequest:
        request = await self.session.scalar(
            select(ExchangeRequest)
            .where(
                ExchangeRequest.public_id == public_id,
                ExchangeRequest.user_id == user_id,
            )
            .with_for_update()
        )
        if request is None:
            raise DomainError("لم يتم العثور على الطلب.")

        is_new_payment_flow = request.status is RequestStatus.PENDING_PAYMENT
        if (
            not is_new_payment_flow
            and request.status is not RequestStatus.AWAITING_REFERENCE
        ):
            raise DomainError("لا يمكن إدخال رقم العملية في هذه المرحلة.")

        await self._ensure_not_expired(request)
        reference = clean_short_text(
            raw_reference,
            "رقم العملية أو معرّف المعاملة",
            maximum_length=128,
        )
        request.payment_reference = reference

        if is_new_payment_flow:
            request.payment_reported_at = utc_now()
            request.status = RequestStatus.PENDING_VERIFICATION
            self.session.add(
                AuditLog(
                    actor_id=user_id,
                    action=AuditAction.REQUEST_PAYMENT_REPORTED,
                    request_public_id=public_id,
                )
            )
        else:
            # إبقاء التدفق القديم متوافقاً إلى أن ننقل معالجاته بالكامل.
            request.status = RequestStatus.AWAITING_AMOUNT

        try:
            await self.session.commit()
        except IntegrityError as error:
            await self.session.rollback()
            raise DuplicatePaymentReferenceError(
                "رقم العملية أو معرّف المعاملة مستخدم مسبقاً. تحقق منه ثم أعد المحاولة."
            ) from error

        return request

    async def set_amount(self, public_id: str, user_id: int, raw_amount: str) -> ExchangeRequest:
        request = await self.get_request_for_user(public_id, user_id, lock=True)
        if request.status is not RequestStatus.AWAITING_AMOUNT:
            raise DomainError("لا يمكن إدخال القيمة في هذه المرحلة.")
        await self._ensure_not_expired(request)
        amount = decimal_from_text(raw_amount)
        received, _ = calculate_route_quote(amount, request.route_code, request.rate_snapshot,
                                            request.fee_percentage_snapshot, request.minimum_usd_snapshot)
        request.from_amount = amount
        request.to_amount = received
        request.status = RequestStatus.AWAITING_PAYOUT_ADDRESS
        await self.session.commit()
        return request

    async def set_target_amount(self, public_id: str, user_id: int, raw_target_amount: str) -> ExchangeRequest:
        request = await self.get_request_for_user(public_id, user_id, lock=True)
        if request.status is not RequestStatus.AWAITING_AMOUNT:
            raise DomainError("لا يمكن إدخال القيمة في هذه المرحلة.")
        await self._ensure_not_expired(request)
        target_amount = decimal_from_text(raw_target_amount)
        payment_amount, _, _, actual_net_amount = calculate_target_breakdown(
            target_amount=target_amount,
            route_code=request.route_code,
            rate=request.rate_snapshot,
            fee_percentage=request.fee_percentage_snapshot,
            minimum_usd=request.minimum_usd_snapshot,
            usdt_per_usd=request.usdt_per_usd_snapshot,
        )
        request.from_amount = payment_amount
        request.to_amount = actual_net_amount
        request.status = RequestStatus.AWAITING_PAYOUT_ADDRESS
        await self.session.commit()
        return request

    async def set_payout_address(
        self, public_id: str, user_id: int, raw_address: str
    ) -> ExchangeRequest:
        request = await self.get_request_for_user(public_id, user_id, lock=True)
        if request.status is not RequestStatus.AWAITING_PAYOUT_ADDRESS:
            raise DomainError("لا يمكن إدخال عنوان الاستلام في هذه المرحلة.")
        await self._ensure_not_expired(request)
        request.payout_address = clean_short_text(raw_address, "عنوان الاستلام")
        await self.session.commit()
        return request

    async def submit_request(self, public_id: str, user_id: int) -> ExchangeRequest:
        request = await self.get_request_for_user(public_id, user_id, lock=True)
        if request.status is not RequestStatus.AWAITING_PAYOUT_ADDRESS:
            raise DomainError("هذا الطلب غير جاهز للتأكيد.")
        await self._ensure_not_expired(request)
        if (
            not request.payment_reference
            or request.from_amount is None
            or request.to_amount is None
            or not request.payout_address
        ):
            raise DomainError("بيانات الطلب غير مكتملة؛ أكملها قبل التأكيد.")

        request.status = RequestStatus.PENDING_REVIEW
        self.session.add(
            AuditLog(
                actor_id=user_id,
                action=AuditAction.REQUEST_SUBMITTED,
                request_public_id=public_id,
            )
        )
        await self.session.commit()
        return request

    async def restart_draft_for_edit(self, public_id: str, user_id: int) -> None:
        request = await self.get_request_for_user(public_id, user_id, lock=True)
        if request.status not in {
            RequestStatus.AWAITING_PAYOUT_ADDRESS,
            RequestStatus.AWAITING_USER_REVIEW,
        }:
            raise DomainError("لا يمكن تعديل الطلب في مرحلته الحالية.")
        await self._ensure_not_expired(request)

        request.payment_reference = None
        request.from_amount = None
        request.to_amount = None
        request.payout_address = None
        request.status = RequestStatus.AWAITING_AMOUNT
        request.expires_at = utc_now() + timedelta(minutes=self.settings.review_ttl_minutes)
        await self.session.commit()

        
    
    async def get_request_for_user(
        self, public_id: str, user_id: int, lock: bool = False
    ) -> ExchangeRequest:
        statement = select(ExchangeRequest).where(
            ExchangeRequest.public_id == public_id, ExchangeRequest.user_id == user_id
        )
        if lock:
            statement = statement.with_for_update()
        request = await self.session.scalar(statement)
        if request is None:
            raise DomainError("لم يتم العثور على الطلب.")
        return request

    async def get_request_for_admin(self, public_id: str, lock: bool = False) -> ExchangeRequest:
        statement = select(ExchangeRequest).where(ExchangeRequest.public_id == public_id)
        if lock:
            statement = statement.with_for_update()
        request = await self.session.scalar(statement)
        if request is None:
            raise DomainError("لم يتم العثور على الطلب.")
        return request

    async def cancel_draft(self, public_id: str, user_id: int) -> None:
        request = await self.get_request_for_user(public_id, user_id, lock=True)
        if request.status not in {
            RequestStatus.AWAITING_REFERENCE,
            RequestStatus.AWAITING_AMOUNT,
            RequestStatus.AWAITING_PAYOUT_ADDRESS,
            RequestStatus.AWAITING_USER_REVIEW,
            RequestStatus.PENDING_PAYMENT,
        }:
            raise DomainError("لا يمكن إلغاء هذا الطلب في مرحلته الحالية.")
        request.status = RequestStatus.CANCELLED
        self.session.add(AuditLog(actor_id=user_id, action=AuditAction.REQUEST_CANCELLED, request_public_id=public_id))
        await self.session.commit()

    async def resolve_request(
        self, public_id: str, actor_id: int, approved: bool
    ) -> ExchangeRequest:
        request = await self.get_request_for_admin(public_id, lock=True)
        if request.status not in {
            RequestStatus.PENDING_REVIEW,
            RequestStatus.PENDING_VERIFICATION,
        }:
            raise DomainError("هذا الطلب عولج سابقاً أو ليس جاهزاً للمراجعة.")
        request.status = RequestStatus.COMPLETED if approved else RequestStatus.REJECTED
        request.reviewed_by = actor_id
        request.reviewed_at = utc_now()
        self.session.add(
            AuditLog(
                actor_id=actor_id,
                action=AuditAction.REQUEST_COMPLETED if approved else AuditAction.REQUEST_REJECTED,
                request_public_id=public_id,
            )
        )
        await self.session.commit()
        return request

    async def pending_user_review_requests(self, limit: int = 10_000) -> list[ExchangeRequest]:
        statement = (
            select(ExchangeRequest)
            .where(
                ExchangeRequest.status.in_({
                    RequestStatus.AWAITING_USER_REVIEW,
                    RequestStatus.EXPIRED,
                    }),
                ExchangeRequest.review_message_id.is_not(None),
            )
            .order_by(ExchangeRequest.expires_at.asc())
            .limit(limit)
        )
        return list((await self.session.scalars(statement)).all())

    async def pending_requests(self, limit: int = 30) -> list[ExchangeRequest]:
        statement = (
            select(ExchangeRequest)
            .where(
                ExchangeRequest.status.in_(
                    {
                        RequestStatus.PENDING_PAYMENT,
                        RequestStatus.PENDING_VERIFICATION,
                        RequestStatus.PENDING_REVIEW,
                    }
                )
            )

            .order_by(ExchangeRequest.created_at.asc())
            .limit(limit)
        )
        return list((await self.session.scalars(statement)).all())

    async def user_requests(self, user_id: int, limit: int = 10) -> list[ExchangeRequest]:
        statement = (
            select(ExchangeRequest)
            .where(ExchangeRequest.user_id == user_id)
            .order_by(ExchangeRequest.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.scalars(statement)).all())

    async def dashboard_stats(self) -> DashboardStats:
        pending_review = await self.session.scalar(
            select(func.count()).select_from(ExchangeRequest).where(
                ExchangeRequest.status == RequestStatus.PENDING_REVIEW
            )
        )
        drafts_in_progress = await self.session.scalar(
            select(func.count()).select_from(ExchangeRequest).where(
                ExchangeRequest.status.in_(
                    [
                        RequestStatus.AWAITING_REFERENCE,
                        RequestStatus.AWAITING_AMOUNT,
                        RequestStatus.AWAITING_PAYOUT_ADDRESS,
                    ]
                )
            )
        )
        start_of_day = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        completed_today = await self.session.scalar(
            select(func.count()).select_from(ExchangeRequest).where(
                ExchangeRequest.status == RequestStatus.COMPLETED,
                ExchangeRequest.reviewed_at >= start_of_day,
            )
        )
        total_users = await self.session.scalar(select(func.count()).select_from(BotUser))
        active_today = await self.session.scalar(select(func.count()).select_from(BotUser).where(BotUser.last_active_at >= start_of_day))
        completed_total = await self.session.scalar(select(func.count()).select_from(ExchangeRequest).where(ExchangeRequest.status == RequestStatus.COMPLETED))
        total_referrals = await self.session.scalar(
            select(func.count()).select_from(ReferralEvent).where(
                ReferralEvent.is_active.is_(True),
                ReferralEvent.eligible_for_levels.is_(True),
            )
        )
        total_withdrawal_paid = await self.session.scalar(
            select(func.coalesce(func.sum(WithdrawalRequest.amount), 0)).where(
                WithdrawalRequest.status == WithdrawalStatus.PAID
            )
        )
        return DashboardStats(
            pending_review=int(pending_review or 0),
            drafts_in_progress=int(drafts_in_progress or 0),
            completed_today=int(completed_today or 0),
            total_users=int(total_users or 0),
            active_today=int(active_today or 0),
            completed_total=int(completed_total or 0),
            total_referrals=int(total_referrals or 0),
            total_withdrawal_paid=Decimal(str(total_withdrawal_paid or 0)),
        )

    async def _upsert_setting(self, key: str, value: str, actor_id: int) -> None:
        setting = await self.session.get(ExchangeSetting, key)
        if setting is None:
            self.session.add(ExchangeSetting(key=key, value=value, updated_by=actor_id))
        else:
            setting.value = value
            setting.updated_by = actor_id

    async def _ensure_not_expired(self, request: ExchangeRequest) -> None:
        if ensure_utc(request.expires_at) <= utc_now():
            request.status = RequestStatus.EXPIRED
            request.review_message_id = None
            await self.session.commit()
            raise DomainError("انتهت صلاحية الطلب. ابدأ طلباً جديداً بالسعر الحالي.")

    async def _new_public_id(self) -> str:
        for _ in range(5):
            candidate = secrets.token_hex(4).upper()
            exists = await self.session.scalar(
                select(ExchangeRequest.id).where(ExchangeRequest.public_id == candidate)
            )
            if exists is None:
                return candidate
        raise RuntimeError("تعذر إنشاء معرّف فريد للطلب؛ أعد المحاولة.")

    async def _new_withdrawal_id(self) -> str:
        for _ in range(20):
            candidate = secrets.token_hex(6).upper()
            exists = await self.session.scalar(
                select(WithdrawalRequest.id).where(WithdrawalRequest.public_id == candidate)
            )
            if exists is None:
                return candidate
        raise DomainError("تعذر إنشاء رقم سحب فريد؛ أعد المحاولة.")

    async def _new_referral_code(self) -> str:
        for _ in range(20):
            candidate = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10].upper()
            exists = await self.session.scalar(
                select(ReferralProfile.user_id).where(ReferralProfile.referral_code == candidate)
            )
            if exists is None:
                return candidate
        raise DomainError("تعذر إنشاء رابط إحالة فريد؛ أعد المحاولة.")