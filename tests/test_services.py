from __future__ import annotations

import asyncio
import os
from pathlib import Path
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import Settings
from app.models import Currency, ReferralEvent, RouteCode, WithdrawalStatus
from app.database import create_engine, create_session_factory, initialize_database
from app.services import (
    ROUTE_DEFAULTS,
    DomainError,
    ExchangeRates,
    calculate_quote,
    calculate_route_quote,
    calculate_payment_for_target,
    clean_short_text,
    decimal_from_text,
    minimum_transfer,
    referral_level_for_count,
    referral_reward_for_level,
)


@pytest.fixture
def rates() -> ExchangeRates:
    return ExchangeRates(
        syp_per_usd=Decimal("10000"),
        usdt_per_usd=Decimal("1"),
        minimum_usd=Decimal("50"),
        completion_eta="من 5 إلى 10 دقائق",
    )


@pytest.mark.parametrize(
    ("amount", "from_currency", "to_currency", "expected"),
    [
        (Decimal("500000"), Currency.SYP, Currency.USD, Decimal("50.00")),
        (Decimal("50"), Currency.USD, Currency.SYP, Decimal("500000")),
        (Decimal("500000"), Currency.SYP, Currency.USDT, Decimal("50.000000")),
        (Decimal("50"), Currency.USD, Currency.USDT, Decimal("50.000000")),
        (Decimal("50"), Currency.USDT, Currency.SYP, Decimal("500000")),
        (Decimal("50"), Currency.USDT, Currency.USD, Decimal("50.00")),
    ],
)
def test_six_routes_are_calculated_correctly(
    rates: ExchangeRates,
    amount: Decimal,
    from_currency: Currency,
    to_currency: Currency,
    expected: Decimal,
) -> None:
    received, usd_equivalent = calculate_quote(amount, from_currency, to_currency, rates)
    assert received == expected
    assert usd_equivalent == Decimal("50.000000")

@pytest.mark.parametrize(
    (
        "target_amount",
        "route_code",
        "rate",
        "fee_percentage",
        "usdt_per_usd",
        "expected_payment",
    ),
    [
        (
            Decimal("60"),
            RouteCode.SYP_TO_USD,
            Decimal("10000"),
            Decimal("0"),
            Decimal("1"),
            Decimal("600000"),
        ),
        (
            Decimal("600000"),
            RouteCode.USD_TO_SYP,
            Decimal("10000"),
            Decimal("0"),
            Decimal("1"),
            Decimal("60.00"),
        ),
        (
            Decimal("60"),
            RouteCode.SYP_TO_USDT,
            Decimal("10000"),
            Decimal("0"),
            Decimal("1"),
            Decimal("600000"),
        ),
        (
            Decimal("54"),
            RouteCode.USD_TO_USDT,
            Decimal("1"),
            Decimal("10"),
            Decimal("1"),
            Decimal("60.00"),
        ),
        (
            Decimal("600000"),
            RouteCode.USDT_TO_SYP,
            Decimal("10000"),
            Decimal("0"),
            Decimal("1"),
            Decimal("60.000000"),
        ),
        (
            Decimal("54"),
            RouteCode.USDT_TO_USD,
            Decimal("1"),
            Decimal("10"),
            Decimal("1"),
            Decimal("60.000000"),
        ),
    ],
)
def test_payment_for_requested_target_on_all_routes(
    target_amount: Decimal,
    route_code: RouteCode,
    rate: Decimal,
    fee_percentage: Decimal,
    usdt_per_usd: Decimal,
    expected_payment: Decimal,
) -> None:
    payment_amount, usd_equivalent = calculate_payment_for_target(
        target_amount=target_amount,
        route_code=route_code,
        rate=rate,
        fee_percentage=fee_percentage,
        minimum_usd=Decimal("50"),
        usdt_per_usd=usdt_per_usd,
    )

    assert payment_amount == expected_payment
    assert usd_equivalent >= Decimal("50.000000")


@pytest.mark.parametrize(
    ("amount", "route_code", "rate", "expected_received"),
    [
        (
            Decimal("600000"),
            RouteCode.SYP_TO_USD,
            Decimal("10000"),
            Decimal("59.40"),
        ),
        (
            Decimal("60"),
            RouteCode.USD_TO_SYP,
            Decimal("10000"),
            Decimal("594000"),
        ),
        (
            Decimal("600000"),
            RouteCode.SYP_TO_USDT,
            Decimal("10000"),
            Decimal("59.400000"),
        ),
        (
            Decimal("60"),
            RouteCode.USD_TO_USDT,
            Decimal("1"),
            Decimal("59.400000"),
        ),
        (
            Decimal("60"),
            RouteCode.USDT_TO_SYP,
            Decimal("10000"),
            Decimal("594000"),
        ),
        (
            Decimal("60"),
            RouteCode.USDT_TO_USD,
            Decimal("1"),
            Decimal("59.40"),
        ),
    ],
)
def test_quote_from_fixed_send_amount_on_all_routes(
    amount: Decimal,
    route_code: RouteCode,
    rate: Decimal,
    expected_received: Decimal,
) -> None:
    received, usd_equivalent = calculate_route_quote(
        amount=amount,
        route_code=route_code,
        rate=rate,
        fee_percentage=Decimal("1"),
        minimum_usd=Decimal("50"),
    )

    assert received == expected_received
    assert usd_equivalent == Decimal("60.000000")



def test_payment_amount_rounds_up_to_valid_currency_unit() -> None:
    payment_amount, _ = calculate_payment_for_target(
        target_amount=Decimal("500001"),
        route_code=RouteCode.USD_TO_SYP,
        rate=Decimal("10000"),
        fee_percentage=Decimal("0"),
        minimum_usd=Decimal("50"),
        usdt_per_usd=Decimal("1"),
    )

    assert payment_amount == Decimal("50.01")


def test_usdt_payment_minimum_uses_usdt_to_usd_rate() -> None:
    with pytest.raises(DomainError, match="الحد الأدنى"):
        calculate_payment_for_target(
            target_amount=Decimal("50"),
            route_code=RouteCode.USDT_TO_USD,
            rate=Decimal("1"),
            fee_percentage=Decimal("0"),
            minimum_usd=Decimal("50"),
            usdt_per_usd=Decimal("1.01"),
        )


def test_target_with_excess_decimal_places_is_rejected() -> None:
    with pytest.raises(DomainError, match="منازل عشرية"):
        calculate_payment_for_target(
            target_amount=Decimal("50.001"),
            route_code=RouteCode.USDT_TO_USD,
            rate=Decimal("1"),
            fee_percentage=Decimal("0"),
            minimum_usd=Decimal("50"),
            usdt_per_usd=Decimal("1"),
        )


def test_minimum_value_is_enforced(rates: ExchangeRates) -> None:
    with pytest.raises(DomainError, match="الحد الأدنى"):
        calculate_quote(Decimal("499999"), Currency.SYP, Currency.USD, rates)


@pytest.mark.parametrize("raw_value", ["", "abc", "-1", "0", "NaN", "Infinity", "1e100"])
def test_invalid_numeric_values_are_rejected(raw_value: str) -> None:
    with pytest.raises(DomainError):
        decimal_from_text(raw_value)


@pytest.mark.parametrize("raw_value", ["", " ", "\x01bad", "x" * 257])
def test_invalid_payout_details_are_rejected(raw_value: str) -> None:
    with pytest.raises(DomainError):
        clean_short_text(raw_value, "عنوان الاستلام")


def test_exactly_six_requested_routes_exist() -> None:
    assert list(ROUTE_DEFAULTS) == [
        RouteCode.SYP_TO_USD,
        RouteCode.USD_TO_SYP,
        RouteCode.SYP_TO_USDT,
        RouteCode.USD_TO_USDT,
        RouteCode.USDT_TO_SYP,
        RouteCode.USDT_TO_USD,
    ]


@pytest.mark.parametrize("admin_ids", [6712633269, "6712633269", "6712633269,123456789", [6712633269]])
def test_single_or_multiple_admin_ids_are_accepted(admin_ids: object) -> None:
    settings = Settings(
        telegram_bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        admin_ids=admin_ids,
        default_syp_per_usd=Decimal("10000"),
        default_usdt_per_usd=Decimal("1"),
        default_usd_to_usdt_fee=Decimal("0"),
        default_usdt_to_usd_fee=Decimal("0"),
        minimum_usd=Decimal("50"),
        database_url="postgresql+asyncpg://user:password@localhost:5432/test_database",
    )
    assert 6712633269 in settings.admin_ids

@pytest.mark.asyncio
async def test_memory_create_target_draft_stores_send_and_receive_amounts(monkeypatch) -> None:
    import app.memory_store as memory_module
    
    from app.memory_store import MemoryService
    from app.models import RequestStatus
    async def fake_live_rate(_route_code: RouteCode) -> Decimal:
        return Decimal("10000")
 
    monkeypatch.setattr(memory_module, "live_rate_for_route", fake_live_rate)
    
    settings = Settings(
        telegram_bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        admin_ids=[6712633269],
        default_syp_per_usd=Decimal("10000"),
        default_usdt_per_usd=Decimal("1"),
        default_usd_to_usdt_fee=Decimal("0"),
        default_usdt_to_usd_fee=Decimal("0"),
        minimum_usd=Decimal("50"),
        storage_mode="memory",
    )

    service = MemoryService(settings)
    await service.bootstrap()
    await service.update_route_address(
        RouteCode.SYP_TO_USD,
        "SYP-PAYMENT-CODE",
        6712633269,
    )
    await service.update_route_barcode(
        RouteCode.SYP_TO_USD,
        "telegram-file-id",
        6712633269,
    )
    await service.set_route_enabled(RouteCode.SYP_TO_USD, True, 6712633269)

    request = await service.create_target_draft(
        route_code=RouteCode.SYP_TO_USD,
        user_id=99,
        chat_id=99,
        username="customer",
        full_name="Test Customer",
        raw_target_amount="60",
    )

    assert request.from_amount is not None
    assert request.from_amount > Decimal("0")
    assert request.to_amount == Decimal("60")
    assert request.status is RequestStatus.AWAITING_PAYOUT_ADDRESS
    assert request.payment_address_snapshot == "SYP-PAYMENT-CODE"
    assert request.barcode_file_id_snapshot == "telegram-file-id"
    before_review = datetime.now(UTC)
    reviewed = await service.prepare_user_review(
        request.public_id,
        99,
        "CUSTOMER-USD-CODE",
    )

    after_review = datetime.now(UTC)

    assert reviewed.status is RequestStatus.AWAITING_USER_REVIEW
    assert reviewed.payout_address == "CUSTOMER-USD-CODE"
    assert (
        before_review + timedelta(minutes=5)
        <= reviewed.expires_at
        <= after_review + timedelta(minutes=5)
    )

    confirmed = await service.confirm_user_review(request.public_id, 99)
    assert confirmed.status is RequestStatus.PENDING_PAYMENT

    before_payment_timer = datetime.now(UTC)
    expires_at = await service.activate_payment_expiry(
        request.public_id,
        99,
        123456,
    )
    after_payment_timer = datetime.now(UTC)

    assert confirmed.payment_message_id == 123456
    assert (
        before_payment_timer + timedelta(minutes=settings.quote_ttl_minutes)
        <= expires_at
        <= after_payment_timer + timedelta(minutes=settings.quote_ttl_minutes)
    )

    reported = await service.set_payment_reference(
        request.public_id,
        99,
        "TX-REPORT-001",
    )
    assert reported.status is RequestStatus.PENDING_VERIFICATION
    assert reported.payment_reference == "TX-REPORT-001"
    assert reported.payment_reported_at is not None

    expiring = await service.create_target_draft(
        route_code=RouteCode.SYP_TO_USD,
        user_id=100,
        chat_id=100,
        username="second_customer",
        full_name="Second Test Customer",
        raw_target_amount="60",
    )
    await service.prepare_user_review(
        expiring.public_id,
        100,
        "SECOND-CUSTOMER-CODE",
    )
    await service.confirm_user_review(expiring.public_id, 100)
    await service.activate_payment_expiry(
        expiring.public_id,
        100,
        123457,
    )

    expiring.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    expired = await service.expire_reference_if_due(expiring.public_id, 100)

    assert expired is True
    assert expiring.status is RequestStatus.EXPIRED

@pytest.mark.asyncio
async def test_postgresql_integration_when_configured() -> None:
    """اختبار اختياري؛ لا يعمل إلا عندما يزوّد المطوّر رابط PostgreSQL تجريبي آمن."""
    test_database_url = os.getenv("TEST_DATABASE_URL")
    if not test_database_url:
        pytest.skip("TEST_DATABASE_URL غير مضبوط؛ اختبارات PostgreSQL التكاملية اختيارية.")

    from app.database import create_engine, create_session_factory, initialize_database
    from app.services import ExchangeService

    settings = Settings(
        telegram_bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        admin_ids=[6712633269],
        default_syp_per_usd=Decimal("10000"),
        default_usdt_per_usd=Decimal("1"),
        default_usd_to_usdt_fee=Decimal("0"),
        default_usdt_to_usd_fee=Decimal("0"),
        minimum_usd=Decimal("50"),
        database_url=test_database_url,
    )
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        await initialize_database(engine)
        async with session_factory() as session:
            service = ExchangeService(session, settings)
            await service.bootstrap()
            assert len(await service.all_routes()) == 6
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_temporary_memory_storage_completes_request_without_database(monkeypatch) -> None:
    import app.memory_store as memory_module
    from app.memory_store import MemoryService
    from app.models import RequestStatus
    async def fake_live_rate(_route_code: RouteCode) -> Decimal:
        return Decimal("10000")
 
    monkeypatch.setattr(memory_module, "live_rate_for_route", fake_live_rate)
    
    settings = Settings(
        telegram_bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        admin_ids=[6712633269],
        default_syp_per_usd=Decimal("10000"),
        default_usdt_per_usd=Decimal("1"),
        default_usd_to_usdt_fee=Decimal("0"),
        default_usdt_to_usd_fee=Decimal("0"),
        minimum_usd=Decimal("50"),
        storage_mode="memory",
    )
    service = MemoryService(settings)
    await service.bootstrap()
    await service.update_route_address(RouteCode.SYP_TO_USD, "SYP-PAYMENT-CODE", 6712633269)
    await service.update_route_barcode(RouteCode.SYP_TO_USD, "telegram-file-id", 6712633269)
    await service.set_route_enabled(RouteCode.SYP_TO_USD, True, 6712633269)

    request = await service.create_draft(
        route_code=RouteCode.SYP_TO_USD,
        user_id=99,
        chat_id=99,
        username="customer",
        full_name="Test Customer",
    )
    await service.set_payment_reference(request.public_id, 99, "TX-001")
    quoted = await service.set_amount(request.public_id, 99, "500000")
    assert quoted.to_amount is not None
    assert quoted.to_amount > Decimal("0")

    submitted = await service.set_payout_address(request.public_id, 99, "CUSTOMER-USD-CODE")
    submitted = await service.submit_request(request.public_id, 99)
    assert submitted.status is RequestStatus.PENDING_REVIEW
    results = await asyncio.gather(
        service.resolve_request(request.public_id, 6712633269, approved=True),
        service.resolve_request(request.public_id, 6712633269, approved=True),
        return_exceptions=True,
    )
    assert sum(isinstance(result, DomainError) for result in results) == 1
    completed = next(result for result in results if not isinstance(result, Exception))
    assert completed.status is RequestStatus.COMPLETED


def test_memory_mode_ignores_legacy_database_url() -> None:
    settings = Settings(
        telegram_bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        admin_ids=[6712633269],
        default_syp_per_usd=Decimal("10000"),
        default_usdt_per_usd=Decimal("1"),
        default_usd_to_usdt_fee=Decimal("0"),
        default_usdt_to_usd_fee=Decimal("0"),
        minimum_usd=Decimal("50"),
        storage_mode="memory",
        database_url="sqlite+aiosqlite:///./legacy.db",
    )
    assert settings.storage_mode == "memory"


def test_postgresql_mode_normalizes_railway_url() -> None:
    settings = Settings(
        telegram_bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        admin_ids=[6712633269],
        default_syp_per_usd=Decimal("10000"),
        default_usdt_per_usd=Decimal("1"),
        default_usd_to_usdt_fee=Decimal("0"),
        default_usdt_to_usd_fee=Decimal("0"),
        minimum_usd=Decimal("50"),
        storage_mode="postgresql",
        database_url="postgresql://user:password@localhost:5432/exchange",
    )
    assert settings.database_url == "postgresql+asyncpg://user:password@localhost:5432/exchange"


@pytest.mark.parametrize(
    ("route", "amount", "rate", "fee", "minimum", "expected"),
    [
        (RouteCode.SYP_TO_USD, Decimal("8100"), Decimal("135"), Decimal("0"), Decimal("60"), Decimal("60")),
        (RouteCode.USD_TO_SYP, Decimal("60"), Decimal("135"), Decimal("0"), Decimal("60"), Decimal("8100")),
        (RouteCode.SYP_TO_USDT, Decimal("8100"), Decimal("135"), Decimal("0"), Decimal("60"), Decimal("60")),
        (RouteCode.USD_TO_USDT, Decimal("60"), Decimal("1"), Decimal("2.5"), Decimal("60"), Decimal("58.5")),
        (RouteCode.USDT_TO_SYP, Decimal("60"), Decimal("135"), Decimal("0"), Decimal("60"), Decimal("8100")),
        (RouteCode.USDT_TO_USD, Decimal("60"), Decimal("1"), Decimal("1"), Decimal("60"), Decimal("59.4")),
    ],
)
def test_route_specific_rates_fees_and_minimums(route, amount, rate, fee, minimum, expected) -> None:
    received, _ = calculate_route_quote(amount, route, rate, fee, minimum)
    assert received == expected


def test_route_minimum_is_rounded_up_to_smallest_transferable_unit() -> None:
    amount, currency = minimum_transfer(RouteCode.SYP_TO_USD, Decimal("60"), Decimal("135.01"))
    assert amount == Decimal("8101")
    assert currency is Currency.SYP


@pytest.mark.asyncio
async def test_memory_admin_users_bans_and_monitoring() -> None:
    from app.memory_store import MemoryService

    settings = Settings(
        telegram_bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        admin_ids=[6712633269],
        default_syp_per_usd=Decimal("10000"),
        default_usdt_per_usd=Decimal("1"),
        default_usd_to_usdt_fee=Decimal("0"),
        default_usdt_to_usd_fee=Decimal("0"),
        minimum_usd=Decimal("50"),
    )
    service = MemoryService(settings)
    await service.bootstrap()

    
    assert not await service.record_user(88, 88, "test_user", "Test User")
    assert await service.user_chat_ids() == [88]

    await service.set_user_banned(88, True)
    assert await service.user_chat_ids() == []

    await service.set_monitoring(True, 7)
    assert await service.monitoring_enabled()

def _referral_test_settings(database_url: str | None = None) -> Settings:
    return Settings(
        telegram_bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        admin_ids=[6712633269],
        default_syp_per_usd=Decimal("10000"),
        default_usdt_per_usd=Decimal("1"),
        default_usd_to_usdt_fee=Decimal("0"),
        default_usdt_to_usd_fee=Decimal("0"),
        minimum_usd=Decimal("50"),
        database_url=database_url,
        referral_level_1_reward=Decimal("2"),
        referral_level_2_reward=Decimal("3"),
        referral_level_3_reward=Decimal("4"),
        referral_level_4_reward=Decimal("5"),
        withdrawal_minimum_usd=Decimal("0.10"),
    )


@pytest.mark.parametrize(
    ("count", "level"),
    [(0, 1), (1, 1), (20, 1), (21, 2), (50, 2), (51, 3), (100, 3), (101, 4)],
)
def test_referral_level_boundaries(count: int, level: int) -> None:
    assert referral_level_for_count(count) == level


def test_new_referral_rewards_default_to_zero() -> None:
    settings = Settings(
        _env_file=None,
        telegram_bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        admin_ids=[6712633269],
        default_syp_per_usd=Decimal("10000"),
        default_usdt_per_usd=Decimal("1"),
        default_usd_to_usdt_fee=Decimal("0"),
        default_usdt_to_usd_fee=Decimal("0"),
        minimum_usd=Decimal("50"),
        storage_mode="memory",
    )
    assert settings.referral_signup_bonus == Decimal("1")
    assert [
        settings.referral_level_1_reward,
        settings.referral_level_2_reward,
        settings.referral_level_3_reward,
        settings.referral_level_4_reward,
    ] == [Decimal("0")] * 4
    assert referral_reward_for_level(settings, 1) == Decimal("0")


@pytest.mark.asyncio
async def test_memory_zero_referral_reward_does_not_auto_pay() -> None:
    from app.memory_store import MemoryService

    settings = _referral_test_settings().model_copy(
        update={
            "referral_level_1_reward": Decimal("0"),
            "referral_level_2_reward": Decimal("0"),
            "referral_level_3_reward": Decimal("0"),
            "referral_level_4_reward": Decimal("0"),
            "storage_mode": "memory",
        }
    )
    service = MemoryService(settings)
    referrer = await service.register_activation(1)
    await service.register_activation(2, referrer.referral_code)
    summary = await service.wallet_summary(1)
    assert summary.balance == Decimal("1")
    assert summary.referrals == 1
    assert summary.referral_reward == Decimal("0")
    assert service._referral_events[0].reward_granted == Decimal("0")


@pytest.mark.asyncio
async def test_old_events_are_historical_and_excluded_from_new_levels(tmp_path: Path) -> None:
    settings = _referral_test_settings(f"sqlite+aiosqlite:///{tmp_path / 'old-events.db'}")
    engine = create_engine(settings.database_url)
    await initialize_database(engine)
    async with create_session_factory(engine)() as session:
        session.add(
            ReferralEvent(
                referrer_user_id=1,
                referred_user_id=2,
                eligible_for_levels=False,
                reward_granted=Decimal("99"),
            )
        )
        await session.commit()
        from app.services import ExchangeService

        summary = await ExchangeService(session, settings).wallet_summary(1)
        assert summary.referrals == 0
        assert summary.referral_level == 1
        assert await ExchangeService(session, settings).referral_admin_leaderboard() == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_migration_is_idempotent_in_temp_database(tmp_path: Path) -> None:
    from sqlalchemy import inspect

    settings = _referral_test_settings(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
    engine = create_engine(settings.database_url)
    await initialize_database(engine)
    await initialize_database(engine)
    async with engine.begin() as connection:
        columns = await connection.run_sync(
            lambda sync_connection: {column["name"] for column in inspect(sync_connection).get_columns("referral_events")}
        )
    assert {"is_active", "revoked_at", "revocation_reason", "eligible_for_levels", "reward_granted", "reward_reversed"} <= columns
    await engine.dispose()


@pytest.mark.asyncio
async def test_new_sql_routes_are_disabled_by_default(tmp_path: Path) -> None:
    from app.services import ExchangeService

    settings = _referral_test_settings(f"sqlite+aiosqlite:///{tmp_path / 'routes-disabled.db'}")
    engine = create_engine(settings.database_url)
    try:
        await initialize_database(engine)
        async with create_session_factory(engine)() as session:
            service = ExchangeService(session, settings)
            await service.bootstrap()
            routes = await service.all_routes()
            assert len(routes) == 6
            assert all(not route.is_enabled for route in routes)
            assert await service.enabled_routes() == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize(("age_seconds", "commit_expected"), [(5, False), (120, True)])
@pytest.mark.asyncio
async def test_record_user_throttles_activity_commits(age_seconds: int, commit_expected: bool) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services import ExchangeService

    now = datetime.now(UTC)
    user = SimpleNamespace(
        chat_id=123,
        username="same-user",
        full_name="Same User",
        last_active_at=now - timedelta(seconds=age_seconds),
        is_banned=True,
    )
    session = SimpleNamespace(get=AsyncMock(return_value=user), commit=AsyncMock())
    service = ExchangeService(session, _referral_test_settings())

    assert await service.record_user(123, 123, "same-user", "Same User") is True
    assert session.commit.await_count == int(commit_expected)


@pytest.mark.parametrize("storage", ["memory", "sql"])
@pytest.mark.asyncio
async def test_set_target_amount_stores_actual_net_after_rounding(monkeypatch, storage: str) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.models import RequestStatus
    from app.services import ExchangeService, calculate_target_breakdown

    settings = _referral_test_settings().model_copy(
        update={"global_fee_percentage": Decimal("1"), "storage_mode": storage}
    )
    request = SimpleNamespace(
        status=RequestStatus.AWAITING_AMOUNT,
        route_code=RouteCode.USD_TO_SYP,
        rate_snapshot=Decimal("13500"),
        fee_percentage_snapshot=Decimal("1"),
        minimum_usd_snapshot=Decimal("50"),
        usdt_per_usd_snapshot=Decimal("1"),
        from_amount=None,
        to_amount=None,
    )
    session = SimpleNamespace(commit=AsyncMock())
    if storage == "memory":
        from app.memory_store import MemoryService

        service = MemoryService(settings)
    else:
        service = ExchangeService(session, settings)
    monkeypatch.setattr(service, "get_request_for_user", AsyncMock(return_value=request))
    monkeypatch.setattr(service, "_ensure_not_expired", AsyncMock())

    result = await service.set_target_amount("REQ-1", 10, "700000")
    expected_payment, _, _, expected_net = calculate_target_breakdown(
        target_amount=Decimal("700000"),
        route_code=RouteCode.USD_TO_SYP,
        rate=Decimal("13500"),
        fee_percentage=Decimal("1"),
        minimum_usd=Decimal("50"),
        usdt_per_usd=Decimal("1"),
    )

    assert result.from_amount == expected_payment
    assert result.to_amount == expected_net
    assert result.to_amount != Decimal("700000")
    if storage == "sql":
        session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_memory_target_amount_update_is_atomic_under_concurrency(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.memory_store import MemoryService
    from app.models import RequestStatus

    request = SimpleNamespace(
        status=RequestStatus.AWAITING_AMOUNT,
        route_code=RouteCode.USD_TO_SYP,
        rate_snapshot=Decimal("13500"),
        fee_percentage_snapshot=Decimal("1"),
        minimum_usd_snapshot=Decimal("50"),
        usdt_per_usd_snapshot=Decimal("1"),
        from_amount=None,
        to_amount=None,
    )
    service = MemoryService(_referral_test_settings().model_copy(update={"storage_mode": "memory"}))
    monkeypatch.setattr(service, "get_request_for_user", AsyncMock(return_value=request))
    monkeypatch.setattr(service, "_ensure_not_expired", AsyncMock())

    results = await asyncio.gather(
        service.set_target_amount("REQ-1", 10, "700000"),
        service.set_target_amount("REQ-1", 10, "700000"),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, DomainError) for result in results) == 1


@pytest.mark.asyncio
async def test_memory_revoke_is_idempotent_and_permanent() -> None:
    from app.memory_store import MemoryService

    service = MemoryService(_referral_test_settings().model_copy(update={"storage_mode": "memory"}))
    referrer = await service.register_activation(1)
    await service.register_activation(2, referrer.referral_code)
    assert service._profiles[1]["referrals"] == 1
    assert await service.revoke_referral_for_referred_user(2, "left")
    assert service._profiles[1]["referrals"] == 0
    assert service._profiles[2]["referrals"] == 0
    balance = (await service.wallet_summary(1)).balance
    assert balance == Decimal("1")
    assert await service.revoke_referral_for_referred_user(2, "left-again")
    assert (await service.wallet_summary(1)).balance == balance
    await service.register_activation(2, referrer.referral_code)
    assert (await service.wallet_summary(1)).referrals == 0


@pytest.mark.asyncio
async def test_persistent_fifo_reversal_respects_pending_rejected_and_paid(tmp_path: Path) -> None:
    from app.services import ExchangeService

    settings = _referral_test_settings(f"sqlite+aiosqlite:///{tmp_path / 'fifo.db'}")
    engine = create_engine(settings.database_url)
    await initialize_database(engine)
    async with create_session_factory(engine)() as session:
        service = ExchangeService(session, settings)
        await service.bootstrap()
        referrer = await service.register_activation(1)
        await service.register_activation(2, referrer.referral_code)
        await service.save_withdrawal_account(1, "test", "account")
        pending = await service.create_withdrawal(1, "1.50")
        assert await service.revoke_referral_for_referred_user(2, "left")
        from app.models import ReferralProfile

        referrer_profile = await session.get(ReferralProfile, 1)
        referred_profile = await session.get(ReferralProfile, 2)
        assert referrer_profile.referral_count == 0
        assert referred_profile.referral_count == 0
        assert (await service.wallet_summary(1)).balance == Decimal("0")
        pending = await service.resolve_withdrawal(pending.public_id, 1, approved=False)
        assert pending.status is WithdrawalStatus.REJECTED
        assert (await service.wallet_summary(1)).balance == Decimal("1")

        await service.register_activation(3, referrer.referral_code)
        paid = await service.create_withdrawal(1, "3")
        paid.review_not_before = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        paid = await service.resolve_withdrawal(paid.public_id, 1, approved=True)
        assert paid.status is WithdrawalStatus.PAID
        assert await service.revoke_referral_for_referred_user(3, "left-after-paid")
        assert (await service.wallet_summary(1)).balance == Decimal("0")
    await engine.dispose()


@pytest.mark.asyncio
async def test_memory_reverses_revoked_referral_reward_after_withdrawal_rejection() -> None:
    from app.memory_store import MemoryService

    settings = _referral_test_settings().model_copy(update={"storage_mode": "memory"})
    service = MemoryService(settings)
    referrer = await service.register_activation(1)
    await service.register_activation(2, referrer.referral_code)
    await service.save_withdrawal_account(1, "test", "account")
    pending = await service.create_withdrawal(1, "1.50")
    assert await service.revoke_referral_for_referred_user(2, "channel_left")
    assert (await service.wallet_summary(1)).balance == Decimal("0")

    await service.resolve_withdrawal(pending.public_id, 1, approved=False)
    # The rejected hold restores the remaining part of the revoked reward, then reverses it.
    assert (await service.wallet_summary(1)).balance == Decimal("1")


@pytest.mark.asyncio
async def test_bootstrap_backfills_legacy_wallet_lots_without_changing_balances(tmp_path: Path) -> None:
    from sqlalchemy import select
    from app.models import Wallet, WalletLedgerEntry, WalletWithdrawalAllocation, WithdrawalRequest
    from app.services import ExchangeService

    settings = _referral_test_settings(f"sqlite+aiosqlite:///{tmp_path / 'legacy-wallet.db'}")
    engine = create_engine(settings.database_url)
    await initialize_database(engine)
    async with create_session_factory(engine)() as session:
        service = ExchangeService(session, settings)
        await service.bootstrap()
        wallet = Wallet(user_id=99, available_balance=Decimal("0.50"), reserved_balance=Decimal("0.50"))
        session.add(wallet)
        session.add(WalletLedgerEntry(
            user_id=99, amount=Decimal("1"), entry_type="SIGNUP_BONUS", idempotency_key="old-signup:99"
        ))
        request = WithdrawalRequest(
            public_id="OLD001",
            user_id=99,
            account_id=1,
            method_snapshot="test",
            account_snapshot="account",
            amount=Decimal("0.50"),
            status=WithdrawalStatus.PENDING_REVIEW,
            review_not_before=datetime.now(UTC) + timedelta(hours=24),
        )
        session.add(request)
        session.add(WalletLedgerEntry(
            user_id=99, amount=Decimal("-0.50"), entry_type="WITHDRAWAL_HOLD", idempotency_key="withdrawal-hold:OLD001"
        ))
        await session.commit()
        original_available = wallet.available_balance
        original_reserved = wallet.reserved_balance

        await service.bootstrap()
        assert wallet.available_balance == original_available
        assert wallet.reserved_balance == original_reserved
        assert await service._wallet_integrity_is_valid(wallet)
        allocations = list((await session.scalars(
            select(WalletWithdrawalAllocation).where(WalletWithdrawalAllocation.withdrawal_request_id == request.id)
        )).all())
        assert sum((item.amount for item in allocations), Decimal("0")) == Decimal("0.50")
        await service.save_withdrawal_account(99, "test", "new-account")
        new_request = await service.create_withdrawal(99, "0.50")
        assert new_request.status is WithdrawalStatus.PENDING_REVIEW
        assert (await service.wallet_summary(99)).balance == Decimal("0")

    await engine.dispose()

@pytest.mark.asyncio
async def test_memory_create_send_amount_draft_stores_send_and_net_receive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.memory_store as memory_store
    from app.memory_store import MemoryService
    from app.models import RequestStatus

    async def fake_live_rate(route_code: RouteCode) -> Decimal:
        assert route_code is RouteCode.USD_TO_USDT
        return Decimal("1")

    monkeypatch.setattr(memory_store, "live_rate_for_route", fake_live_rate)

    settings = Settings(
        telegram_bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        admin_ids=[6712633269],
        default_syp_per_usd=Decimal("10000"),
        default_usdt_per_usd=Decimal("1"),
        default_usd_to_usdt_fee=Decimal("0"),
        default_usdt_to_usd_fee=Decimal("0"),
        global_fee_percentage=Decimal("1"),
        minimum_usd=Decimal("50"),
        storage_mode="memory",
    )

    service = MemoryService(settings)
    await service.bootstrap()

    await service.update_route_address(
        RouteCode.USD_TO_USDT,
        "USDT-TEST-ADDRESS",
        6712633269,
    )
    await service.update_route_barcode(
        RouteCode.USD_TO_USDT,
        "test-file-id",
        6712633269,
    )
    await service.set_route_enabled(RouteCode.USD_TO_USDT, True, 6712633269)

    request = await service.create_send_amount_draft(
        route_code=RouteCode.USD_TO_USDT,
        user_id=99,
        chat_id=99,
        username="customer",
        full_name="Test Customer",
        raw_send_amount="60",
    )

    assert request.from_amount == Decimal("60")
    assert request.to_amount == Decimal("59.4")
    assert request.fee_percentage_snapshot == Decimal("1")
    assert request.rate_snapshot == Decimal("1")
    assert request.minimum_usd_snapshot == Decimal("50")
    assert request.status is RequestStatus.AWAITING_PAYOUT_ADDRESS

def test_target_calculator_keyboard_modes() -> None:
    from app.keyboards import target_calculator_menu

    initial = target_calculator_menu(mode="receive")
    initial_callbacks = [
        button.callback_data
        for row in initial.inline_keyboard
        for button in row
    ]
    assert "quote:mode:receive" in initial_callbacks
    assert "quote:mode:send" in initial_callbacks
    assert "flow:cancel" in initial_callbacks

    send_preview = target_calculator_menu(preview=True, mode="send")
    preview_callbacks = [
        button.callback_data
        for row in send_preview.inline_keyboard
        for button in row
    ]
    assert "quote:continue" in preview_callbacks
    assert "quote:change" in preview_callbacks
    assert "quote:refresh" in preview_callbacks
    assert "quote:mode:receive" not in preview_callbacks
    assert "flow:cancel" in preview_callbacks

    edit_screen = target_calculator_menu(show_modes=False)
    edit_callbacks = [
        button.callback_data
        for row in edit_screen.inline_keyboard
        for button in row
    ]
    assert "quote:mode:receive" not in edit_callbacks
    assert "quote:mode:send" not in edit_callbacks


@pytest.mark.asyncio
async def test_sqlite_create_send_amount_draft_stores_send_and_net_receive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services as services_module
    from app.models import RequestStatus
    from app.services import ExchangeService

    async def fake_live_rate(route_code: RouteCode) -> Decimal:
        assert route_code is RouteCode.USD_TO_USDT
        return Decimal("1")

    monkeypatch.setattr(
        services_module,
        "live_rate_for_route",
        fake_live_rate,
    )

    settings = Settings(
        telegram_bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        admin_ids=[6712633269],
        default_syp_per_usd=Decimal("10000"),
        default_usdt_per_usd=Decimal("1"),
        default_usd_to_usdt_fee=Decimal("0"),
        default_usdt_to_usd_fee=Decimal("0"),
        global_fee_percentage=Decimal("1"),
        minimum_usd=Decimal("50"),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'send-draft.db'}",
    )

    engine = create_engine(settings.database_url)

    try:
        await initialize_database(engine)
        session_factory = create_session_factory(engine)

        async with session_factory() as session:
            service = ExchangeService(session, settings)
            await service.bootstrap()

            await service.update_route_address(
                RouteCode.USD_TO_USDT,
                "USDT-TEST-ADDRESS",
                6712633269,
            )
            await service.update_route_barcode(
                RouteCode.USD_TO_USDT,
                "test-file-id",
                6712633269,
            )
            await service.set_route_enabled(RouteCode.USD_TO_USDT, True, 6712633269)

            request = await service.create_send_amount_draft(
                route_code=RouteCode.USD_TO_USDT,
                user_id=99,
                chat_id=99,
                username="customer",
                full_name="Test Customer",
                raw_send_amount="60",
            )

            assert request.from_amount == Decimal("60")
            assert request.to_amount == Decimal("59.4")
            assert request.fee_percentage_snapshot == Decimal("1")
            assert request.rate_snapshot == Decimal("1")
            assert request.minimum_usd_snapshot == Decimal("50")
            assert request.status is RequestStatus.AWAITING_PAYOUT_ADDRESS
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_memory_set_payment_proof_moves_request_to_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.memory_store as memory_store
    from app.memory_store import MemoryService
    from app.models import RequestStatus

    async def fake_live_rate(route_code: RouteCode) -> Decimal:
        assert route_code is RouteCode.USD_TO_USDT
        return Decimal("1")

    monkeypatch.setattr(memory_store, "live_rate_for_route", fake_live_rate)

    settings = Settings(
        telegram_bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        admin_ids=[6712633269],
        default_syp_per_usd=Decimal("10000"),
        default_usdt_per_usd=Decimal("1"),
        default_usd_to_usdt_fee=Decimal("0"),
        default_usdt_to_usd_fee=Decimal("0"),
        global_fee_percentage=Decimal("1"),
        minimum_usd=Decimal("50"),
        storage_mode="memory",
    )

    service = MemoryService(settings)
    await service.bootstrap()

    await service.update_route_address(
        RouteCode.USD_TO_USDT,
        "USDT-TEST-ADDRESS",
        6712633269,
    )
    await service.update_route_barcode(
        RouteCode.USD_TO_USDT,
        "test-barcode-id",
        6712633269,
    )
    await service.set_route_enabled(RouteCode.USD_TO_USDT, True, 6712633269)

    request = await service.create_target_draft(
        route_code=RouteCode.USD_TO_USDT,
        user_id=99,
        chat_id=99,
        username="customer",
        full_name="Test Customer",
        raw_target_amount="59.4",
    )
    await service.prepare_user_review(request.public_id, 99, "CUSTOMER-RECEIVE-ADDRESS")
    await service.confirm_user_review(request.public_id, 99)
    await service.activate_payment_expiry(request.public_id, 99, 123456)

    reported = await service.set_payment_proof(
        request.public_id,
        99,
        "telegram-proof-file-id",
        "photo",
    )

    assert reported.status is RequestStatus.PENDING_VERIFICATION
    assert reported.payment_proof_file_id == "telegram-proof-file-id"
    assert reported.payment_proof_type == "photo"
    assert reported.payment_reported_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("proof_type", ["photo", "pdf"])
async def test_sqlite_set_payment_proof_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    proof_type: str,
) -> None:
    import app.services as services_module
    from app.models import ExchangeRequest, RequestStatus
    from app.services import ExchangeService

    async def fake_live_rate(route_code: RouteCode) -> Decimal:
        assert route_code is RouteCode.USD_TO_USDT
        return Decimal("1")

    monkeypatch.setattr(
        services_module,
        "live_rate_for_route",
        fake_live_rate,
    )

    settings = Settings(
        telegram_bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        admin_ids=[6712633269],
        default_syp_per_usd=Decimal("10000"),
        default_usdt_per_usd=Decimal("1"),
        default_usd_to_usdt_fee=Decimal("0"),
        default_usdt_to_usd_fee=Decimal("0"),
        global_fee_percentage=Decimal("1"),
        minimum_usd=Decimal("50"),
        database_url=(
            f"sqlite+aiosqlite:///{tmp_path / f'proof-{proof_type}.db'}"
        ),
    )

    engine = create_engine(settings.database_url)
    try:
        await initialize_database(engine)
        session_factory = create_session_factory(engine)

        async with session_factory() as session:
            service = ExchangeService(session, settings)
            await service.bootstrap()

            await service.update_route_address(
                RouteCode.USD_TO_USDT,
                "USDT-TEST-ADDRESS",
                6712633269,
            )
            await service.update_route_barcode(
                RouteCode.USD_TO_USDT,
                "test-barcode-id",
                6712633269,
            )
            await service.set_route_enabled(RouteCode.USD_TO_USDT, True, 6712633269)

            request = await service.create_target_draft(
                route_code=RouteCode.USD_TO_USDT,
                user_id=99,
                chat_id=99,
                username="customer",
                full_name="Test Customer",
                raw_target_amount="59.4",
            )
            await service.prepare_user_review(
                request.public_id,
                99,
                "CUSTOMER-RECEIVE-ADDRESS",
                crypto_method="TRC20",
            )
            await service.confirm_user_review(request.public_id, 99)
            await service.activate_payment_expiry(
                request.public_id,
                99,
                123456,
            )

            reported = await service.set_payment_proof(
                request.public_id,
                99,
                f"test-{proof_type}-file-id",
                proof_type,
            )

            assert reported.status is RequestStatus.PENDING_VERIFICATION
            await session.refresh(reported)

            assert reported.payment_proof_file_id == f"test-{proof_type}-file-id"
            assert reported.payment_proof_type == proof_type
            assert reported.payment_reported_at is not None

            saved = await session.get(ExchangeRequest, request.id)
            assert saved is not None
            assert saved.status is RequestStatus.PENDING_VERIFICATION
            assert saved.payment_proof_file_id == f"test-{proof_type}-file-id"
            assert saved.payment_proof_type == proof_type
    finally:
        await engine.dispose()
        
def test_usdt_amount_format_is_limited_to_two_decimal_places() -> None:
    from app.services import format_amount
 
    assert format_amount(Decimal("1.239"), Currency.USDT) == "1.23 USDT"
    assert format_amount(Decimal("1.20"), Currency.USDT) == "1.2 USDT"
 
 
def test_crypto_method_detail_menu_has_admin_actions() -> None:
    from app.keyboards import crypto_method_detail_menu
 
    keyboard = crypto_method_detail_menu("TRC20")
    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    }
    assert callbacks == {
        "admin:crypto-address:TRC20",
        "admin:crypto-photo:TRC20",
        "admin:crypto-methods",
        "admin:home",
    }
 
 
@pytest.mark.asyncio
async def test_force_refresh_bypasses_fresh_exchange_rate_cache(monkeypatch) -> None:
    import asyncio
    import app.services as services_module
 
    monkeypatch.setattr(
        services_module,
        "_SP_TODAY_CACHE",
        (Decimal("100"), Decimal("101"), datetime.now(UTC)),
    )
    monkeypatch.setattr(services_module, "_SP_TODAY_CACHE_LOCK", asyncio.Lock())
    calls: list[str] = []
 
    class FakeResponse:
        async def __aenter__(self):
            return self
 
        async def __aexit__(self, *_args):
            return None
 
        def raise_for_status(self) -> None:
            return None
 
        async def json(self, content_type=None):
            return {"data": {"currencies": {"USD:damascus": {"buy": "12345", "sell": "12456"}}}}
 
    class FakeSession:
        def __init__(self, *, timeout):
            self.timeout = timeout
 
        async def __aenter__(self):
            return self
 
        async def __aexit__(self, *_args):
            return None
 
        def get(self, _url, headers=None):
            calls.append("fetch")
            return FakeResponse()
 
    monkeypatch.setattr(services_module.aiohttp, "ClientSession", FakeSession)
    buy, sell, _updated_at = await services_module.get_sp_today_usd_rates(force_refresh=True)
 
    assert calls == ["fetch"]
    assert buy == Decimal("123.45")
    assert sell == Decimal("124.56")
 
 
@pytest.mark.asyncio
async def test_memory_set_amount_rejects_excess_decimal_places() -> None:
    from app.memory_store import MemoryService
    from app.models import RequestStatus
 
    settings = Settings(
        telegram_bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        admin_ids=[6712633269],
        default_syp_per_usd=Decimal("10000"),
        default_usdt_per_usd=Decimal("1"),
        default_usd_to_usdt_fee=Decimal("0"),
        default_usdt_to_usd_fee=Decimal("0"),
        global_fee_percentage=Decimal("1"),
        minimum_usd=Decimal("50"),
        storage_mode="memory",
    )
    service = MemoryService(settings)
    await service.bootstrap()
    await service.update_route_address(RouteCode.USD_TO_USDT, "TEST-ADDRESS", 6712633269)
    await service.update_route_barcode(RouteCode.USD_TO_USDT, "test-file-id", 6712633269)
    await service.set_route_enabled(RouteCode.USD_TO_USDT, True, 6712633269)
    request = await service.create_draft(
        route_code=RouteCode.USD_TO_USDT,
        user_id=99,
        chat_id=99,
        username="customer",
        full_name="Test Customer",
    )
    await service.set_payment_reference(request.public_id, 99, "TEST-TX-1")
 
    with pytest.raises(DomainError, match="المنازل العشرية"):
        await service.set_amount(request.public_id, 99, "50.001")
 
    assert request.status is RequestStatus.AWAITING_AMOUNT
    assert request.from_amount is None
 
 
@pytest.mark.asyncio
async def test_sqlite_set_amount_rejects_excess_decimal_places(tmp_path: Path) -> None:
    from app.models import ExchangeRequest, RequestStatus
    from app.services import ExchangeService
 
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'precision.db'}"
    settings = Settings(
        telegram_bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        admin_ids=[6712633269],
        default_syp_per_usd=Decimal("10000"),
        default_usdt_per_usd=Decimal("1"),
        default_usd_to_usdt_fee=Decimal("0"),
        default_usdt_to_usd_fee=Decimal("0"),
        global_fee_percentage=Decimal("1"),
        minimum_usd=Decimal("50"),
        storage_mode="sqlite",
        database_url=database_url,
    )
    engine = create_engine(database_url)
    session_factory = create_session_factory(engine)
    try:
        await initialize_database(engine)
        async with session_factory() as session:
            service = ExchangeService(session, settings)
            await service.bootstrap()
            await service.update_route_address(RouteCode.USD_TO_USDT, "TEST-ADDRESS", 6712633269)
            await service.update_route_barcode(RouteCode.USD_TO_USDT, "test-file-id", 6712633269)
            await service.set_route_enabled(RouteCode.USD_TO_USDT, True, 6712633269)
            request = await service.create_draft(
                route_code=RouteCode.USD_TO_USDT,
                user_id=99,
                chat_id=99,
                username="customer",
                full_name="Test Customer",
            )
            await service.set_payment_reference(request.public_id, 99, "TEST-TX-2")
 
            with pytest.raises(DomainError, match="المنازل العشرية"):
                await service.set_amount(request.public_id, 99, "50.001")
 
            saved = await session.get(ExchangeRequest, request.id)
            assert saved is not None
            assert saved.status is RequestStatus.AWAITING_AMOUNT
            assert saved.from_amount is None
    finally:
        await engine.dispose()
 
 
@pytest.mark.asyncio
async def test_global_minimum_cannot_be_changed_as_database_rate() -> None:
    from app.memory_store import MemoryService
 
    settings = Settings(
        telegram_bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        admin_ids=[6712633269],
        default_syp_per_usd=Decimal("10000"),
        default_usdt_per_usd=Decimal("1"),
        default_usd_to_usdt_fee=Decimal("0"),
        default_usdt_to_usd_fee=Decimal("0"),
        global_fee_percentage=Decimal("1"),
        minimum_usd=Decimal("50"),
        storage_mode="memory",
    )
    service = MemoryService(settings)
    with pytest.raises(DomainError, match="MINIMUM_USD"):
        await service.update_rate("minimum_usd", "25", 6712633269)
 
 
def test_payment_proof_size_limit_is_enforced() -> None:
    from app.handlers import MAX_PAYMENT_PROOF_BYTES, payment_proof_file_size_allowed
 
    assert not payment_proof_file_size_allowed(None)
    assert not payment_proof_file_size_allowed(0)
    assert payment_proof_file_size_allowed(MAX_PAYMENT_PROOF_BYTES)
    assert not payment_proof_file_size_allowed(MAX_PAYMENT_PROOF_BYTES + 1)


def test_payment_proof_pdf_content_signature_is_checked() -> None:
    from app.handlers import payment_proof_pdf_signature_valid

    prefix = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    xref_offset = len(prefix)
    pdf = (
        prefix
        + b"xref\n0 2\n0000000000 65535 f \n0000000009 00000 n \n"
        + b"trailer\n<< /Size 2 /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode("ascii")
        + b"\n%%EOF\n"
    )
    assert payment_proof_pdf_signature_valid(pdf)
    assert not payment_proof_pdf_signature_valid(b"not-a-pdf.pdf\n%%EOF")
    assert not payment_proof_pdf_signature_valid(b"%PDF-1.7\nmissing eof")
    assert not payment_proof_pdf_signature_valid(b"%PDF-1.7\nbody\nstartxref\n9\n%%EOF")
