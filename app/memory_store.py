"""تخزين مؤقت في الذاكرة للتجربة المحلية فقط.

تُفقد جميع البيانات عند إغلاق البرنامج أو إعادة تشغيله.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

from app.config import Settings
from app.models import Currency, RequestStatus, RouteCode
from app.services import (
    ROUTE_DEFAULTS,
    USD_QUANTUM,
    DashboardStats,
    DomainError,
    calculate_target_breakdown,
    DuplicatePaymentReferenceError,
    ExchangeRates,
    LeaderboardEntry,
    WalletSummary,
    calculate_quote,
    clean_short_text,
    decimal_nonnegative_from_text,
    decimal_from_text,
    format_amount,
    quantum_for,
    calculate_route_quote,
    calculate_payment_for_target,
    utc_now,
)
from app.static_routes import load_static_routes
from app.services import live_rate_for_route


@dataclass
class MemoryRoute:
    code: RouteCode
    from_currency: Currency
    to_currency: Currency
    display_name: str
    rate_value: Decimal
    minimum_usd: Decimal
    payment_address: str | None = None
    barcode_file_id: str | None = None
    fee_percentage: Decimal = Decimal("0")
    is_enabled: bool = False
    updated_by: int | None = None


@dataclass
class MemoryRequest:
    public_id: str
    route_code: RouteCode
    user_id: int
    chat_id: int
    username: str | None
    full_name: str
    from_currency: Currency
    to_currency: Currency
    payment_address_snapshot: str
    syp_per_usd_snapshot: Decimal
    usdt_per_usd_snapshot: Decimal
    minimum_usd_snapshot: Decimal
    rate_snapshot: Decimal
    fee_percentage_snapshot: Decimal
    eta_snapshot: str
    expires_at: datetime
    payment_reference: str | None = None
    payment_proof_file_id: str | None = None
    payment_proof_type: str | None = None
    barcode_file_id_snapshot: str | None = None
    payment_reported_at: datetime | None = None
    payment_message_id: int | None = None
    review_message_id: int | None = None
    from_amount: Decimal | None = None
    to_amount: Decimal | None = None
    payout_address: str | None = None
    crypto_method: str | None = None
    status: RequestStatus = RequestStatus.AWAITING_REFERENCE
    reviewed_by: int | None = None
    reviewed_at: datetime | None = None
    created_at: datetime | None = None


@dataclass
class MemoryWithdrawal:
    public_id: str
    user_id: int
    method_snapshot: str
    account_snapshot: str
    amount: Decimal
    review_not_before: datetime
    status: str = "PENDING_REVIEW"
    allocations: list[tuple[int, Decimal]] = field(default_factory=list)


@dataclass
class MemoryReferralEvent:
    referrer_user_id: int
    referred_user_id: int
    created_at: datetime
    is_active: bool = True
    revoked_at: datetime | None = None
    revocation_reason: str | None = None
    eligible_for_levels: bool = True
    reward_granted: Decimal = Decimal("0")
    reward_reversed: Decimal = Decimal("0")


@dataclass
class MemoryLedgerEntry:
    user_id: int
    amount: Decimal
    entry_type: str
    idempotency_key: str


@dataclass
class MemoryCreditLot:
    lot_id: int
    user_id: int
    ledger_key: str
    original_amount: Decimal
    remaining_amount: Decimal
    created_at: datetime


class MemoryService:
    """واجهة خدمة متوافقة مع معالجات البوت، لكنها لا تحفظ شيئاً على القرص."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()
        self._routes: dict[RouteCode, MemoryRoute] = {}
        self._requests: dict[str, MemoryRequest] = {}
        self._settings: dict[str, str] = {}
        self._users: dict[int, dict[str, object]] = {}
        self._profiles: dict[int, dict[str, object]] = {}
        self._wallets: dict[int, Decimal] = {}
        self._reserved_wallets: dict[int, Decimal] = {}
        self._accounts: dict[int, tuple[str, str]] = {}
        self._referral_events: list[MemoryReferralEvent] = []
        self._wallet_ledger: dict[str, MemoryLedgerEntry] = {}
        self._credit_lots: list[MemoryCreditLot] = []
        self._next_lot_id = 1
        self._withdrawals: dict[str, MemoryWithdrawal] = {}

    async def bootstrap(self) -> None:
        async with self._lock:
            static_routes = load_static_routes()
            if not self._settings:
                self._settings = {
                    "syp_per_usd": str(self.settings.default_syp_per_usd),
                    "usdt_per_usd": str(self.settings.default_usdt_per_usd),
                    "minimum_usd": str(self.settings.minimum_usd),
                    "completion_eta": self.settings.completion_eta,
                }
            for code, (from_currency, to_currency, display_name) in ROUTE_DEFAULTS.items():
                route = self._routes.setdefault(
                    code,
                    MemoryRoute(
                        code=code,
                        from_currency=from_currency,
                        to_currency=to_currency,
                        display_name=display_name,
                        rate_value=(self.settings.default_syp_per_usd if Currency.SYP in (from_currency, to_currency) else self.settings.default_usdt_per_usd),
                        minimum_usd=self.settings.minimum_usd,
                        fee_percentage=(self.settings.default_usd_to_usdt_fee if code is RouteCode.USD_TO_USDT
                                        else self.settings.default_usdt_to_usd_fee if code is RouteCode.USDT_TO_USD
                                        else Decimal("0")),
                        is_enabled=False,
                    ),
                )
                if not route.payment_address:
                    route.payment_address = static_routes[code].payment_address
                if not route.barcode_file_id:
                    route.barcode_file_id = str(static_routes[code].barcode_path)
            if self._settings.get("route_fee_defaults_initialized") != "1":
                self._routes[RouteCode.USD_TO_USDT].fee_percentage = self.settings.default_usd_to_usdt_fee
                self._routes[RouteCode.USDT_TO_USD].fee_percentage = self.settings.default_usdt_to_usd_fee
                self._settings["route_fee_defaults_initialized"] = "1"

    async def current_rates(self) -> ExchangeRates:
        await self.bootstrap()
        return ExchangeRates(
            syp_per_usd=Decimal(self._settings["syp_per_usd"]),
            usdt_per_usd=Decimal(self._settings["usdt_per_usd"]),
            minimum_usd=self.settings.minimum_usd,
            completion_eta=self.settings.completion_eta,
        )

    async def all_routes(self) -> list[MemoryRoute]:
        await self.bootstrap()
        return [self._routes[code] for code in RouteCode]

    async def enabled_routes(self) -> list[MemoryRoute]:
        return [route for route in await self.all_routes() if route.is_enabled]

    async def get_route(self, route_code: RouteCode) -> MemoryRoute:
        await self.bootstrap()
        try:
            return self._routes[route_code]
        except KeyError as error:
            raise DomainError("مسار التحويل غير موجود.") from error

    async def update_rate(self, key: str, raw_value: str, actor_id: int) -> Decimal:
        if key == "minimum_usd":
            raise DomainError("الحد الأدنى موحّد ويُدار من MINIMUM_USD في ملف البيئة.")
        if key not in {"syp_per_usd", "usdt_per_usd"}:
            raise DomainError("إعداد السعر غير صالح.")
        value = decimal_from_text(raw_value)
        async with self._lock:
            self._settings[key] = str(value)
        return value

    async def record_user(self, user_id: int, chat_id: int, username: str | None, full_name: str) -> bool:
        item = self._users.setdefault(user_id, {"chat_id": chat_id, "is_banned": False, "first": utc_now()})
        item.update(chat_id=chat_id, username=username, full_name=full_name, last=utc_now())
        return bool(item["is_banned"])

    def _referral_level(self, count: int) -> int:
        from app.services import referral_level_for_count
        return referral_level_for_count(count)

    def _referral_reward(self, level: int) -> Decimal:
        from app.services import referral_reward_for_level
        return referral_reward_for_level(self.settings, level)

    def _active_level_count(self, referrer_user_id: int) -> int:
        return sum(
            1
            for event in self._referral_events
            if event.referrer_user_id == referrer_user_id
            and event.is_active
            and event.eligible_for_levels
        )

    def _referral_would_cycle(self, referrer_user_id: int, user_id: int) -> bool:
        """يتحقق من أن ربط المستخدم لا ينشئ دورة في سلسلة المحيلين."""
        current_user_id: int | None = referrer_user_id
        visited: set[int] = set()
        while current_user_id is not None and current_user_id not in visited:
            if current_user_id == user_id:
                return True
            visited.add(current_user_id)
            profile = self._profiles.get(current_user_id)
            current_user_id = profile.get("referrer") if profile else None
        return False

    def _credit_wallet(self, user_id: int, amount: Decimal, entry_type: str, key: str) -> None:
        if key in self._wallet_ledger:
            return
        amount = Decimal(str(amount))
        if amount < 0:
            raise DomainError("قيمة credit غير صالحة.")
        self._wallets[user_id] = self._wallets.get(user_id, Decimal("0")) + amount
        self._wallet_ledger[key] = MemoryLedgerEntry(user_id, amount, entry_type, key)
        if amount > 0:
            self._credit_lots.append(
                MemoryCreditLot(
                    self._next_lot_id,
                    user_id,
                    key,
                    amount,
                    amount,
                    utc_now(),
                )
            )
            self._next_lot_id += 1

    def _wallet_integrity_is_valid(self, user_id: int) -> bool:
        available = self._wallets.get(user_id, Decimal("0"))
        ledger_total = sum(
            (entry.amount for entry in self._wallet_ledger.values() if entry.user_id == user_id),
            Decimal("0"),
        )
        lots_total = sum(
            (lot.remaining_amount for lot in self._credit_lots if lot.user_id == user_id),
            Decimal("0"),
        )
        pending_reserved = sum(
            (amount for request in self._withdrawals.values() if request.user_id == user_id and request.status == "PENDING_REVIEW" for _, amount in request.allocations),
            Decimal("0"),
        )
        return (
            available == ledger_total == lots_total
            and self._reserved_wallets.get(user_id, Decimal("0")) == pending_reserved
            and available >= 0
            and self._reserved_wallets.get(user_id, Decimal("0")) >= 0
        )

    async def register_activation(self, user_id: int, referral_code: str | None = None) -> WalletSummary:
        async with self._lock:
            profile = self._profiles.get(user_id)
            if profile is None:
                code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10].upper()
                profile = {"code": code, "referrer": None, "referrals": 0}
                self._profiles[user_id] = profile
                self._wallets[user_id] = Decimal("0")
                self._reserved_wallets[user_id] = Decimal("0")
            self._credit_wallet(
                user_id,
                self.settings.referral_signup_bonus,
                "SIGNUP_BONUS",
                f"signup:{user_id}",
            )
            normalized_code = referral_code.strip().upper() if referral_code else None
            if normalized_code and profile["referrer"] is None:
                referrer = next((uid for uid, item in self._profiles.items() if item["code"] == normalized_code), None)
                existing_event = next((event for event in self._referral_events if event.referred_user_id == user_id), None)
                if (
                    referrer is not None
                    and referrer != user_id
                    and not self._referral_would_cycle(referrer, user_id)
                ):
                    profile["referrer"] = existing_event.referrer_user_id if existing_event else referrer
                    if existing_event is None:
                        count = self._active_level_count(referrer) + 1
                        level = self._referral_level(count)
                        reward = self._referral_reward(level)
                        self._profiles[referrer]["referrals"] = max(0, int(self._profiles[referrer]["referrals"])) + 1
                        event = MemoryReferralEvent(referrer, user_id, utc_now(), reward_granted=reward)
                        self._referral_events.append(event)
                        if reward > 0:
                            self._credit_wallet(
                                referrer,
                                reward,
                                "REFERRAL_REWARD",
                                f"referral:{referrer}:{user_id}",
                            )
            return await self.wallet_summary(user_id)

    async def wallet_summary(self, user_id: int) -> WalletSummary:
        if user_id not in self._profiles:
            await self.register_activation(user_id)
        account = self._accounts.get(user_id)
        profile = self._profiles[user_id]
        count = self._active_level_count(user_id)
        level = self._referral_level(count)
        return WalletSummary(
            balance=self._wallets.get(user_id, Decimal("0")),
            reserved=self._reserved_wallets.get(user_id, Decimal("0")),
            referrals=count,
            referral_code=str(profile["code"]),
            account_method=account[0] if account else None,
            account_value=account[1] if account else None,
            withdrawal_minimum=self.settings.withdrawal_minimum_usd,
            referral_level=level,
            referral_reward=self._referral_reward(level),
        )

    def _reverse_available_revoked_referral_rewards(self, referrer_user_id: int) -> None:
        if not self._wallet_integrity_is_valid(referrer_user_id):
            return
        wallet = self._wallets.get(referrer_user_id, Decimal("0"))
        for event in self._referral_events:
            if event.referrer_user_id != referrer_user_id or event.is_active:
                continue
            remaining = max(Decimal("0"), event.reward_granted - event.reward_reversed)
            if remaining <= 0:
                continue
            lot = next(
                (
                    item for item in self._credit_lots
                    if item.user_id == referrer_user_id
                    and item.ledger_key == f"referral:{referrer_user_id}:{event.referred_user_id}"
                ),
                None,
            )
            if lot is None:
                continue
            amount = min(remaining, lot.remaining_amount, wallet)
            if amount <= 0:
                continue
            key = f"referral-reversal:{referrer_user_id}:{event.referred_user_id}:{event.reward_reversed}"
            if key in self._wallet_ledger:
                continue
            lot.remaining_amount -= amount
            wallet -= amount
            event.reward_reversed += amount
            self._wallet_ledger[key] = MemoryLedgerEntry(
                referrer_user_id, -amount, "REFERRAL_REWARD_REVERSAL", key
            )
        self._wallets[referrer_user_id] = wallet

    async def revoke_referral_for_referred_user(self, referred_user_id: int, reason: str) -> bool:
        async with self._lock:
            event = next((item for item in self._referral_events if item.referred_user_id == referred_user_id), None)
            if event is None:
                return False
            if event.is_active:
                event.is_active = False
                event.revoked_at = utc_now()
                event.revocation_reason = clean_short_text(reason, "سبب إلغاء الإحالة", maximum_length=512)
                profile = self._profiles.get(event.referrer_user_id)
                if profile is not None:
                    profile["referrals"] = max(0, int(profile["referrals"]) - 1)
            elif not event.revocation_reason and reason:
                event.revocation_reason = clean_short_text(reason, "سبب إلغاء الإحالة", maximum_length=512)
            self._reverse_available_revoked_referral_rewards(event.referrer_user_id)
            return True

    async def referral_admin_leaderboard(self, limit: int = 50) -> list[LeaderboardEntry]:
        counts: dict[int, int] = {}
        for event in self._referral_events:
            if event.is_active and event.eligible_for_levels:
                counts[event.referrer_user_id] = counts.get(event.referrer_user_id, 0) + 1
        ranked = sorted(
            (
                (uid, count, str(self._users.get(uid, {}).get("full_name", f"مستخدم {uid}")))
                for uid, count in counts.items()
                if count > 0
            ),
            key=lambda item: (-item[1], item[0]),
        )[:limit]
        return [LeaderboardEntry(uid, name, count) for uid, count, name in ranked]

    async def save_withdrawal_account(self, user_id: int, method: str, account_value: str) -> object:
        method = clean_short_text(method, "طريقة السحب", 32)
        account_value = clean_short_text(account_value, "حساب السحب", 256)
        self._accounts[user_id] = (method, account_value)
        return self._accounts[user_id]

    async def create_withdrawal(self, user_id: int, amount_text: str) -> MemoryWithdrawal:
        raw_amount = decimal_from_text(amount_text)
        amount = raw_amount.quantize(USD_QUANTUM, rounding=ROUND_DOWN)
        if amount != raw_amount:
            raise DomainError("مبلغ السحب لا يقبل أكثر من منزلتين عشريتين.")

        if amount < self.settings.withdrawal_minimum_usd:
            raise DomainError("الحد الأدنى للسحب غير محقق.")
        async with self._lock:
            if user_id not in self._accounts:
                raise DomainError("الرصيد المتاح غير كافٍ أو لم تضف حساب السحب.")
            if not self._wallet_integrity_is_valid(user_id):
                raise DomainError("تعذر التحقق من دفتر المحفظة؛ لم يُخصم أي رصيد.")
            lots = [
                item for item in self._credit_lots
                if item.user_id == user_id and item.remaining_amount > 0
            ]
            lots.sort(key=lambda item: (item.created_at, item.lot_id))
            if sum((item.remaining_amount for item in lots), Decimal("0")) < amount:
                raise DomainError("الرصيد المتاح غير كافٍ أو لم تضف حساب السحب.")
            public_id = secrets.token_hex(6).upper()
            request = MemoryWithdrawal(
                public_id,
                user_id,
                self._accounts[user_id][0],
                self._accounts[user_id][1],
                amount,
                utc_now() + timedelta(hours=self.settings.withdrawal_review_hours),
            )
            left = amount
            for lot in lots:
                if left <= 0:
                    break
                allocation = min(left, lot.remaining_amount)
                if allocation <= 0:
                    continue
                lot.remaining_amount -= allocation
                request.allocations.append((lot.lot_id, allocation))
                left -= allocation
            if left != 0:
                raise DomainError("تعذر تخصيص السحب محاسبياً؛ لم يُخصم أي رصيد.")
            self._wallets[user_id] -= amount
            self._reserved_wallets[user_id] = self._reserved_wallets.get(user_id, Decimal("0")) + amount
            self._wallet_ledger[f"withdrawal-hold:{public_id}"] = MemoryLedgerEntry(
                user_id, -amount, "WITHDRAWAL_HOLD", f"withdrawal-hold:{public_id}"
            )
            self._withdrawals[public_id] = request
            return request

    async def pending_withdrawals(self, limit: int = 50) -> list[MemoryWithdrawal]:
        return [request for request in self._withdrawals.values() if request.status == "PENDING_REVIEW"][:limit]

    async def resolve_withdrawal(self, public_id: str, actor_id: int, approved: bool, note: str | None = None) -> MemoryWithdrawal:
        async with self._lock:
            request = self._withdrawals.get(public_id)
            if request is None or request.status != "PENDING_REVIEW":
                raise DomainError("طلب السحب غير متاح للمراجعة.")
            if approved and request.review_not_before > utc_now():
                raise DomainError("لم تنتهِ فترة المراجعة الأمنية لهذا الطلب بعد.")
            if not self._wallet_integrity_is_valid(request.user_id):
                raise DomainError("تعذر التحقق من دفتر المحفظة؛ لم يتغير الطلب.")
            if sum((amount for _, amount in request.allocations), Decimal("0")) != request.amount:
                raise DomainError("تخصيص طلب السحب غير متطابق؛ لم يتغير الطلب.")
            self._reserved_wallets[request.user_id] -= request.amount
            if self._reserved_wallets[request.user_id] < 0:
                raise DomainError("رصيد الحجز غير متطابق؛ لم يتغير الطلب.")
            request.status = "PAID" if approved else "REJECTED"
            if not approved:
                for lot_id, allocation in request.allocations:
                    lot = next((item for item in self._credit_lots if item.lot_id == lot_id), None)
                    if lot is None:
                        raise DomainError("credit lot مفقود؛ لم يتغير الطلب.")
                    lot.remaining_amount += allocation
                self._wallets[request.user_id] += request.amount
                key = f"withdrawal-release:{request.public_id}"
                self._wallet_ledger[key] = MemoryLedgerEntry(
                    request.user_id, request.amount, "WITHDRAWAL_RELEASE", key
                )
                self._reverse_available_revoked_referral_rewards(request.user_id)
            else:
                key = f"withdrawal-paid:{request.public_id}"
                self._wallet_ledger[key] = MemoryLedgerEntry(
                    request.user_id, Decimal("0"), "WITHDRAWAL_PAID", key
                )
            return request

    async def is_banned(self, user_id: int) -> bool:
        return bool(self._users.get(user_id, {}).get("is_banned", False))

    async def set_user_banned(self, user_id: int, banned: bool, actor_id: int | None = None) -> None:
        if user_id not in self._users:
            if not banned: raise DomainError("لم يُسجّل هذا المستخدم في البوت.")
            self._users[user_id] = {"chat_id": user_id, "is_banned": True, "first": utc_now(), "last": utc_now(),
                                    "full_name": "مستخدم محظور", "username": None}
            return
        self._users[user_id]["is_banned"] = banned

    async def user_chat_ids(self) -> list[int]:
        return [int(v["chat_id"]) for v in self._users.values() if not v["is_banned"]]

    async def monitoring_enabled(self) -> bool:
        return self._settings.get("monitoring_enabled") == "1"

    async def set_monitoring(self, enabled: bool, actor_id: int) -> None:
        self._settings["monitoring_enabled"] = "1" if enabled else "0"

    async def get_setting_value(self, key: str, default: str = "") -> str:
        return self._settings.get(key, default)

    async def set_setting_value(self, key: str, value: str, actor_id: int) -> None:
        self._settings[key] = value

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
        return (
            self._settings.get(f"crypto_method:{method}:address"),
            self._settings.get(f"crypto_method:{method}:barcode"),
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
        async with self._lock:
            self._settings[f"crypto_method:{method}:address"] = value
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
        async with self._lock:
            self._settings[f"crypto_method:{method}:barcode"] = file_id

    async def update_route_address(self, route_code: RouteCode, raw_value: str, actor_id: int) -> str:
        route = await self.get_route(route_code)
        value = clean_short_text(raw_value, "عنوان أو كود الدفع")
        async with self._lock:
            route.payment_address = value
            route.updated_by = actor_id
        return value

    async def update_route_barcode(self, route_code: RouteCode, file_id: str, actor_id: int) -> None:
        if not file_id or len(file_id) > 256:
            raise DomainError("صورة الباركود غير صالحة.")
        route = await self.get_route(route_code)
        async with self._lock:
            route.barcode_file_id = file_id
            route.updated_by = actor_id

    async def set_route_enabled(self, route_code: RouteCode, enabled: bool, actor_id: int) -> None:
        route = await self.get_route(route_code)
        async with self._lock:
            route.is_enabled = enabled
            route.updated_by = actor_id

    async def create_draft(
        self,
        *,
        route_code: RouteCode,
        user_id: int,
        chat_id: int,
        username: str | None,
        full_name: str,
    ) -> MemoryRequest:
        route = await self.get_route(route_code)
        if not route.is_enabled:
            raise DomainError("هذا المسار متوقف مؤقتاً.")
        if not route.payment_address or not route.barcode_file_id:
            raise DomainError("هذا المسار غير مهيأ حالياً. تواصل مع الإدارة.")
        live_rate = await live_rate_for_route(route.code)
        rates = await self.current_rates()
        request = MemoryRequest(
            public_id=await self._new_public_id(),
            route_code=route.code,
            user_id=user_id,
            chat_id=chat_id,
            username=username[:64] if username else None,
            full_name=full_name[:256],
            from_currency=route.from_currency,
            to_currency=route.to_currency,
            payment_address_snapshot=route.payment_address,
            syp_per_usd_snapshot=rates.syp_per_usd,
            usdt_per_usd_snapshot=Decimal("1"),
            minimum_usd_snapshot=rates.minimum_usd,
            rate_snapshot=live_rate,
            fee_percentage_snapshot=self.settings.global_fee_percentage,
            eta_snapshot=rates.completion_eta,
            expires_at=utc_now() + timedelta(minutes=self.settings.quote_ttl_minutes),
            created_at=utc_now(),
        )
        async with self._lock:
            self._requests[request.public_id] = request
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
    ) -> MemoryRequest:
        route = await self.get_route(route_code)
        if not route.is_enabled:
            raise DomainError("هذا المسار متوقف مؤقتاً.")
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

        now = utc_now()
        request = MemoryRequest(
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
            from_amount=payment_amount,
            to_amount=actual_net_amount,
            syp_per_usd_snapshot=rates.syp_per_usd,
            usdt_per_usd_snapshot=rates.usdt_per_usd,
            minimum_usd_snapshot=rates.minimum_usd,
            rate_snapshot=live_rate,
            fee_percentage_snapshot=self.settings.global_fee_percentage,
            eta_snapshot=rates.completion_eta,
            status=RequestStatus.AWAITING_PAYOUT_ADDRESS,
            expires_at=now + timedelta(minutes=self.settings.review_ttl_minutes),
            created_at=now,
        )
        async with self._lock:
            self._requests[request.public_id] = request
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
    ) -> MemoryRequest:
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

        now = utc_now()
        request = MemoryRequest(
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
            expires_at=now + timedelta(minutes=self.settings.review_ttl_minutes),
            created_at=now,
        )
        async with self._lock:
            self._requests[request.public_id] = request
        return request

    async def prepare_user_review(
        self,
        public_id: str,
        user_id: int,
        raw_payout_address: str,
        crypto_method: str | None = None,
    ) -> MemoryRequest:
        request = await self.get_request_for_user(public_id, user_id)
        if request.status is not RequestStatus.AWAITING_PAYOUT_ADDRESS:
            raise DomainError("لا يمكن تجهيز مراجعة الطلب في هذه المرحلة.")
        if request.expires_at <= utc_now():
            async with self._lock:
                if request.status is RequestStatus.AWAITING_PAYOUT_ADDRESS:
                    request.status = RequestStatus.EXPIRED
            raise DomainError("انتهت صلاحية الطلب. ابدأ طلباً جديداً بالسعر الحالي.")
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
            if crypto_method is not None and crypto_method not in allowed_methods:
                raise DomainError("اختر طريقة استلام USDT أولاً.")
            request.crypto_method = crypto_method if crypto_method in allowed_methods else None
        elif request.from_currency is Currency.USDT:
            if crypto_method not in allowed_methods:
                raise DomainError("اختر طريقة إرسال USDT أولاً.")
            address = self._settings.get(f"crypto_method:{crypto_method}:address")
            barcode = self._settings.get(f"crypto_method:{crypto_method}:barcode")
            if not address or not barcode:
                raise DomainError(
                    "طريقة USDT المختارة غير مجهزة بعنوان الدفع وصورة QR. "
                    "اختر طريقة أخرى أو تواصل مع الإدارة."
                )
            request.crypto_method = crypto_method
            request.payment_address_snapshot = address
            request.barcode_file_id_snapshot = barcode
        else:
            request.crypto_method = None

        async with self._lock:
            if request.status is not RequestStatus.AWAITING_PAYOUT_ADDRESS:
                raise DomainError("تغيّرت حالة الطلب؛ أعد المحاولة.")
            request.payout_address = payout_address
            request.status = RequestStatus.AWAITING_USER_REVIEW
            request.expires_at = utc_now() + timedelta(
                minutes=self.settings.review_ttl_minutes
            )

        return request

    async def set_user_review_message_id(
        self,
        public_id: str,
        user_id: int,
        message_id: int,
    ) -> None:
        if message_id <= 0:
            raise DomainError("رقم رسالة المراجعة غير صالح.")
        request = await self.get_request_for_user(public_id, user_id)
        async with self._lock:
            if request.status is not RequestStatus.AWAITING_USER_REVIEW:
                raise DomainError("لم يعد الطلب بانتظار مراجعة المستخدم.")
            request.review_message_id = message_id

    async def confirm_user_review(
        self,
        public_id: str,
        user_id: int,
    ) -> MemoryRequest:
        request = await self.get_request_for_user(public_id, user_id)

        async with self._lock:
            if request.status is not RequestStatus.AWAITING_USER_REVIEW:
                raise DomainError("لم يعد هذا الطلب بانتظار تأكيدك.")

            if request.expires_at <= utc_now():
                request.status = RequestStatus.EXPIRED
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

        return request

    async def expire_user_review_if_due(
        self,
        public_id: str,
        user_id: int,
    ) -> bool:
        async with self._lock:
            request = self._requests.get(public_id)
            if request is None or request.user_id != user_id:
                return False
            if request.status not in {
                RequestStatus.AWAITING_USER_REVIEW,
                RequestStatus.EXPIRED,
            }:
                return False
            if request.expires_at > utc_now():
                return False

            if request.status is RequestStatus.AWAITING_USER_REVIEW:
                request.status = RequestStatus.EXPIRED
            request.review_message_id = None
            return True

    async def activate_payment_expiry(
        self,
        public_id: str,
        user_id: int,
        message_id: int,
    ) -> datetime:
        request = await self.get_request_for_user(public_id, user_id)

        if message_id <= 0:
            raise DomainError("رقم رسالة الدفع غير صالح.")

        async with self._lock:
            if request.status is not RequestStatus.PENDING_PAYMENT:
                raise DomainError("الطلب ليس في مرحلة انتظار الدفع.")

            if request.payment_message_id is not None:
                if request.payment_message_id == message_id:
                    return request.expires_at
                raise DomainError("تم تسجيل رسالة الدفع لهذا الطلب مسبقاً.")

            request.payment_message_id = message_id
            request.expires_at = utc_now() + timedelta(
                minutes=self.settings.quote_ttl_minutes
            )
            return request.expires_at


    async def activate_reference_expiry(
        self, public_id: str, user_id: int
    ) -> datetime:
        request = await self.get_request_for_user(public_id, user_id)

        async with self._lock:
            if request.status is not RequestStatus.AWAITING_REFERENCE:
                raise DomainError("لم يعد الطلب بانتظار رقم العملية.")

            request.expires_at = utc_now() + timedelta(
                minutes=self.settings.quote_ttl_minutes
            )
            return request.expires_at


    async def expire_reference_if_due(
        self,
        public_id: str,
        user_id: int,
    ) -> bool:
        async with self._lock:
            request = self._requests.get(public_id)
            if request is None or request.user_id != user_id:
                return False

            if request.status not in {
                RequestStatus.AWAITING_REFERENCE,
                RequestStatus.PENDING_PAYMENT,
            }:
                return False

            if request.expires_at > utc_now():
                return False

            request.status = RequestStatus.EXPIRED
            return True


    async def set_payment_proof(
        self,
        public_id: str,
        user_id: int,
        file_id: str,
        file_type: str,
    ) -> MemoryRequest:
        request = await self.get_request_for_user(public_id, user_id)

        proof_type = file_type.strip().lower()
        proof_file_id = file_id.strip()
        if proof_type not in {"photo", "pdf"} or not proof_file_id or len(proof_file_id) > 256:
            raise DomainError("نوع ملف إثبات الدفع غير صالح.")

        async with self._lock:
            if request.status is not RequestStatus.PENDING_PAYMENT:
                raise DomainError("لا يمكن إرسال إثبات الدفع في هذه المرحلة.")

            if request.expires_at <= utc_now():
                request.status = RequestStatus.EXPIRED
                raise DomainError("انتهت صلاحية بيانات الدفع. ابدأ طلباً جديداً.")

            request.payment_proof_file_id = proof_file_id
            request.payment_proof_type = proof_type
            request.payment_reported_at = utc_now()
            request.status = RequestStatus.PENDING_VERIFICATION

        return request

    
    async def set_payment_reference(
        self,
        public_id: str,
        user_id: int,
        raw_reference: str,
    ) -> MemoryRequest:
        request = await self.get_request_for_user(public_id, user_id)
        is_new_payment_flow = request.status is RequestStatus.PENDING_PAYMENT

        if (
            not is_new_payment_flow
            and request.status is not RequestStatus.AWAITING_REFERENCE
        ):
            raise DomainError("لا يمكن إدخال رقم العملية في هذه المرحلة.")

        reference = clean_short_text(
            raw_reference,
            "رقم العملية أو معرّف المعاملة",
            maximum_length=128,
        )

        async with self._lock:
            expected_status = (
                RequestStatus.PENDING_PAYMENT
                if is_new_payment_flow
                else RequestStatus.AWAITING_REFERENCE
            )
            if request.status is not expected_status:
                raise DomainError("تغيّرت حالة الطلب؛ لا يمكن تسجيل المرجع الآن.")

            if request.expires_at <= utc_now():
                request.status = RequestStatus.EXPIRED
                raise DomainError("انتهت صلاحية بيانات الدفع. ابدأ طلباً جديداً.")

            if any(
                item.payment_reference == reference
                and item.public_id != request.public_id
                for item in self._requests.values()
            ):
                raise DuplicatePaymentReferenceError(
                    "رقم العملية أو معرّف المعاملة مستخدم مسبقاً. تحقق منه ثم أعد المحاولة."
                )

            request.payment_reference = reference

            if is_new_payment_flow:
                request.payment_reported_at = utc_now()
                request.status = RequestStatus.PENDING_VERIFICATION
            else:
                # إبقاء التدفق القديم متوافقاً مؤقتاً.
                request.status = RequestStatus.AWAITING_AMOUNT

        return request

    async def set_amount(self, public_id: str, user_id: int, raw_amount: str) -> MemoryRequest:
        request = await self.get_request_for_user(public_id, user_id)
        if request.status is not RequestStatus.AWAITING_AMOUNT:
            raise DomainError("لا يمكن إدخال القيمة في هذه المرحلة.")
        await self._ensure_not_expired(request)
        amount = decimal_from_text(raw_amount)
        received, _ = calculate_route_quote(amount, request.route_code, request.rate_snapshot,
                                            request.fee_percentage_snapshot, request.minimum_usd_snapshot)
        async with self._lock:
            request.from_amount = amount
            request.to_amount = received
            request.status = RequestStatus.AWAITING_PAYOUT_ADDRESS
        return request

    async def set_target_amount(self, public_id: str, user_id: int, raw_target_amount: str) -> MemoryRequest:
        request = await self.get_request_for_user(public_id, user_id)
        async with self._lock:
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
        return request

    async def set_payout_address(self, public_id: str, user_id: int, raw_address: str) -> MemoryRequest:
        request = await self.get_request_for_user(public_id, user_id)
        if request.status is not RequestStatus.AWAITING_PAYOUT_ADDRESS:
            raise DomainError("لا يمكن إدخال عنوان الاستلام في هذه المرحلة.")
        await self._ensure_not_expired(request)
        async with self._lock:
            request.payout_address = clean_short_text(raw_address, "عنوان الاستلام")
        return request
        


    async def submit_request(self, public_id: str, user_id: int) -> MemoryRequest:
        request = await self.get_request_for_user(public_id, user_id)
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

        async with self._lock:
            request.status = RequestStatus.PENDING_REVIEW
        return request

    async def restart_draft_for_edit(self, public_id: str, user_id: int) -> None:
        request = await self.get_request_for_user(public_id, user_id)
        if request.status not in {
            RequestStatus.AWAITING_PAYOUT_ADDRESS,
            RequestStatus.AWAITING_USER_REVIEW,
        }:
            raise DomainError("لا يمكن تعديل الطلب في مرحلته الحالية.")
        await self._ensure_not_expired(request)

        async with self._lock:
            request.payment_reference = None
            request.from_amount = None
            request.to_amount = None
            request.payout_address = None
            request.status = RequestStatus.AWAITING_AMOUNT
            request.expires_at = utc_now() + timedelta(minutes=self.settings.review_ttl_minutes)

    async def get_request_for_user(self, public_id: str, user_id: int) -> MemoryRequest:
        request = self._requests.get(public_id)
        if request is None or request.user_id != user_id:
            raise DomainError("لم يتم العثور على الطلب.")
        return request

    async def get_request_for_admin(self, public_id: str, lock: bool = False) -> MemoryRequest:
        request = self._requests.get(public_id)
        if request is None:
            raise DomainError("لم يتم العثور على الطلب.")
        return request

    async def cancel_draft(self, public_id: str, user_id: int) -> None:
        request = await self.get_request_for_user(public_id, user_id)
        if request.status not in {
            RequestStatus.AWAITING_REFERENCE,
            RequestStatus.AWAITING_AMOUNT,
            RequestStatus.AWAITING_PAYOUT_ADDRESS,
        }:
            raise DomainError("لا يمكن إلغاء هذا الطلب في مرحلته الحالية.")
        async with self._lock:
            request.status = RequestStatus.CANCELLED

    async def resolve_request(self, public_id: str, actor_id: int, approved: bool) -> MemoryRequest:
        async with self._lock:
            request = await self.get_request_for_admin(public_id)
            if request.status not in {
                RequestStatus.PENDING_REVIEW,
                RequestStatus.PENDING_VERIFICATION,
            }:
                raise DomainError("هذا الطلب عولج سابقاً أو ليس جاهزاً للمراجعة.")
            request.status = RequestStatus.COMPLETED if approved else RequestStatus.REJECTED
            request.reviewed_by = actor_id
            request.reviewed_at = utc_now()
        return request

    async def pending_requests(self, limit: int = 30) -> list[MemoryRequest]:
        return sorted(
            [
                item
                for item in self._requests.values()
                if item.status
                in {
                    RequestStatus.PENDING_PAYMENT,
                    RequestStatus.PENDING_VERIFICATION,
                    RequestStatus.PENDING_REVIEW,
                }
            ],

            key=lambda item: item.created_at or utc_now(),
        )[:limit]

    async def pending_user_review_requests(self, limit: int = 10_000) -> list[MemoryRequest]:
        return sorted(
            [
                item
                for item in self._requests.values()
                if item.status in {
                    RequestStatus.AWAITING_USER_REVIEW,
                    RequestStatus.EXPIRED,
                }
                and item.review_message_id is not None
            ],
            key=lambda item: item.expires_at,
        )[:limit]

    async def user_requests(self, user_id: int, limit: int = 10) -> list[MemoryRequest]:
        return sorted(
            [item for item in self._requests.values() if item.user_id == user_id],
            key=lambda item: item.created_at or utc_now(),
            reverse=True,
        )[:limit]

    async def dashboard_stats(self) -> DashboardStats:
        requests = list(self._requests.values())
        day_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        return DashboardStats(
            pending_review=sum(item.status is RequestStatus.PENDING_REVIEW for item in requests),
            drafts_in_progress=sum(
                item.status
                in {
                    RequestStatus.AWAITING_REFERENCE,
                    RequestStatus.AWAITING_AMOUNT,
                    RequestStatus.AWAITING_PAYOUT_ADDRESS,
                }
                for item in requests
            ),
            completed_today=sum(
                item.status is RequestStatus.COMPLETED
                and item.reviewed_at is not None
                and item.reviewed_at >= day_start
                for item in requests
            ),
            total_users=len(self._users),
            active_today=sum(item.get("last", day_start) >= day_start for item in self._users.values()),
            completed_total=sum(item.status is RequestStatus.COMPLETED for item in requests),
            total_referrals=sum(
                item.is_active and item.eligible_for_levels for item in self._referral_events
            ),
            total_withdrawal_paid=sum(
                (item.amount for item in self._withdrawals.values() if item.status == "PAID"),
                Decimal("0"),
            ),
        )

    async def _ensure_not_expired(self, request: MemoryRequest) -> None:
        if request.expires_at <= utc_now():
            async with self._lock:
                request.status = RequestStatus.EXPIRED
            raise DomainError("انتهت صلاحية الطلب. ابدأ طلباً جديداً بالسعر الحالي.")

    async def _new_public_id(self) -> str:
        async with self._lock:
            for _ in range(5):
                candidate = secrets.token_hex(4).upper()
                if candidate not in self._requests:
                    return candidate
        raise RuntimeError("تعذر إنشاء معرّف فريد للطلب؛ أعد المحاولة.")
