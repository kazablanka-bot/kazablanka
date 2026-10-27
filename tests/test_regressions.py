from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine as create_sync_engine, select

from app.config import Settings
from app.database import create_engine, create_session_factory, initialize_database
from app.keyboards import admin_home_menu, admin_withdrawal_list_menu
from app.memory_store import MemoryService
from app.models import Base, WalletLedgerEntry
from app.services import ExchangeService


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "telegram_bot_token": "123456:TEST_TOKEN_DO_NOT_USE",
        "admin_ids": [123456789],
        "default_syp_per_usd": Decimal("10000"),
        "default_usdt_per_usd": Decimal("1"),
        "default_usd_to_usdt_fee": Decimal("0"),
        "default_usdt_to_usd_fee": Decimal("0"),
        "minimum_usd": Decimal("50"),
        "storage_mode": "memory",
    }
    values.update(overrides)
    return Settings(**values)


def test_global_fee_precision_matches_database_snapshot_scale() -> None:
    assert make_settings(global_fee_percentage=Decimal("1.23000")).global_fee_percentage == Decimal("1.23000")
    with pytest.raises(ValueError):
        make_settings(global_fee_percentage=Decimal("1.23001"))


@pytest.mark.asyncio
async def test_memory_referral_is_case_insensitive_and_idempotent() -> None:
    settings = make_settings(referral_level_1_reward=Decimal("0.15"))
    service = MemoryService(settings)
    await service.bootstrap()

    referrer = await service.register_activation(1)
    referred = await service.register_activation(2, referrer.referral_code.lower())
    repeated = await service.register_activation(2, referrer.referral_code)
    referrer_after = await service.wallet_summary(1)

    assert referred.referrals == 0
    assert repeated.referrals == 0
    assert referrer_after.referrals == 1
    assert referrer_after.balance == Decimal("1.15")


@pytest.mark.asyncio
async def test_memory_admin_referrals_and_paid_withdrawals_are_reported() -> None:
    settings = make_settings(withdrawal_minimum_usd=Decimal("1"))
    service = MemoryService(settings)
    await service.bootstrap()
    referrer = await service.register_activation(1)
    await service.register_activation(2, referrer.referral_code)
    await service.save_withdrawal_account(1, "Crypto", "wallet-address")

    # Add a test credit through the ledger so the FIFO integrity invariant remains valid.
    service._credit_wallet(1, Decimal("5"), "TEST_CREDIT", "test-credit:1")
    withdrawal = await service.create_withdrawal(1, "1")
    withdrawal.review_not_before = withdrawal.review_not_before.replace(year=2000)
    await service.resolve_withdrawal(withdrawal.public_id, 123456789, approved=True)

    rows = await service.referral_admin_leaderboard()
    stats = await service.dashboard_stats()
    assert rows[0].user_id == 1
    assert rows[0].referrals == 1
    assert stats.total_referrals == 1
    assert stats.total_withdrawal_paid == Decimal("1.00")


def test_zero_rewards_are_allowed_and_admin_ids_must_be_positive() -> None:
    settings = make_settings(
        referral_signup_bonus=Decimal("0"),
        referral_level_1_reward=Decimal("0"),
        referral_level_2_reward=Decimal("0"),
        referral_level_3_reward=Decimal("0"),
        referral_level_4_reward=Decimal("0"),
    )
    assert settings.referral_signup_bonus == Decimal("0")

    with pytest.raises(ValueError, match="ADMIN_IDS"):
        make_settings(admin_ids=[0])


@pytest.mark.asyncio
async def test_sqlite_initialization_migrates_missing_columns_and_enables_pragmas(tmp_path) -> None:
    database_path = tmp_path / "legacy.db"
    sync_engine = create_sync_engine(f"sqlite:///{database_path}")
    try:
        Base.metadata.create_all(sync_engine)
        with sync_engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE exchange_routes DROP COLUMN rate_value")
            connection.exec_driver_sql("ALTER TABLE exchange_routes DROP COLUMN fee_percentage")
            connection.exec_driver_sql("ALTER TABLE exchange_routes DROP COLUMN minimum_usd")
            connection.exec_driver_sql("ALTER TABLE exchange_requests DROP COLUMN barcode_file_id_snapshot")
            connection.exec_driver_sql("ALTER TABLE exchange_requests DROP COLUMN payment_reported_at")
            connection.exec_driver_sql(
                "ALTER TABLE exchange_requests DROP COLUMN payment_proof_file_id"
            )
            connection.exec_driver_sql(
                "ALTER TABLE exchange_requests DROP COLUMN payment_proof_type"
            )
            connection.exec_driver_sql("ALTER TABLE exchange_requests DROP COLUMN payment_message_id")
            connection.exec_driver_sql("ALTER TABLE exchange_requests DROP COLUMN rate_snapshot")
            connection.exec_driver_sql("ALTER TABLE exchange_requests DROP COLUMN fee_percentage_snapshot")
    finally:
        sync_engine.dispose()

    async_engine = create_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        await initialize_database(async_engine)
        async with async_engine.connect() as connection:
            assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar() == 1
            assert (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar().lower() == "wal"
    finally:
        await async_engine.dispose()

    with sqlite3.connect(database_path) as connection:
        route_columns = {row[1] for row in connection.execute("PRAGMA table_info(exchange_routes)")}
        request_columns = {row[1] for row in connection.execute("PRAGMA table_info(exchange_requests)")}
    assert {"rate_value", "fee_percentage", "minimum_usd"} <= route_columns
    assert {
        "barcode_file_id_snapshot",
        "payment_reported_at",
        "payment_proof_file_id",
        "payment_proof_type",
        "payment_message_id",
        "rate_snapshot",
        "fee_percentage_snapshot",
    } <= request_columns


@pytest.mark.asyncio
async def test_sqlite_referral_and_withdrawal_are_persisted_with_audit_entries(tmp_path) -> None:
    database_path = tmp_path / "persistent.db"
    settings = make_settings(
        storage_mode="sqlite",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        withdrawal_minimum_usd=Decimal("1"),
        referral_level_1_reward=Decimal("0.15"),
    )
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        await initialize_database(engine)
        async with session_factory() as session:
            service = ExchangeService(session, settings)
            await service.bootstrap()
            referrer = await service.register_activation(1)
            await service.register_activation(2, referrer.referral_code.lower())
            summary = await service.wallet_summary(1)
            assert summary.referrals == 1
            assert summary.balance == Decimal("1.15")

            await service.save_withdrawal_account(1, "Crypto", "wallet-address")
            request = await service.create_withdrawal(1, "1")
            request.review_not_before = datetime.now(UTC) - timedelta(minutes=1)
            await session.commit()
            await service.resolve_withdrawal(request.public_id, 123456789, approved=False)

            ledger = list((await session.scalars(select(WalletLedgerEntry))).all())
            assert {entry.entry_type for entry in ledger} >= {
                "WITHDRAWAL_HOLD",
                "WITHDRAWAL_RELEASE",
            }
            assert request.status.value == "REJECTED"
    finally:
        await engine.dispose()


def test_admin_keyboards_expose_general_rates_and_each_withdrawal() -> None:
    home_callbacks = {
        button.callback_data
        for row in admin_home_menu().inline_keyboard
        for button in row
        if button.callback_data
    }
    withdrawal_callbacks = {
        button.callback_data
        for row in admin_withdrawal_list_menu(["AAA", "BBB"]).inline_keyboard
        for button in row
        if button.callback_data
    }
    assert "admin:rates" in home_callbacks
    assert "admin:withdrawal:view:AAA" in withdrawal_callbacks
    assert "admin:withdrawal:approve:AAA" in withdrawal_callbacks
    assert "admin:withdrawal:reject:BBB" in withdrawal_callbacks

@pytest.mark.parametrize("mode", ["receive", "send"])
@pytest.mark.asyncio
async def test_change_target_quote_uses_route_from_fsm_state(monkeypatch, mode: str) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
 
    import app.handlers as handlers
    from app.models import RouteCode
 
    monkeypatch.setattr(handlers, "is_private", lambda _callback: True)
    callback = SimpleNamespace(
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=99),
            message_id=50,
            edit_text=AsyncMock(),
        ),
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={
            "route_code": RouteCode.USD_TO_USDT.value,
            "quote_mode": mode,
            "prompt_chat_id": 99,
            "prompt_message_id": 50,
        }),
        get_state=AsyncMock(return_value=handlers.UserFlow.awaiting_target_amount.state),
        update_data=AsyncMock(),
    )
 
    await handlers.change_target_quote(callback, state)
 
    callback.message.edit_text.assert_awaited_once()
    assert handlers.ROUTE_LABELS[RouteCode.USD_TO_USDT] in callback.message.edit_text.await_args.args[0]
    state.update_data.assert_awaited_once_with(
        target_amount_input=None,
        target_quote_ready=False,
    )


@pytest.mark.asyncio
async def test_choose_route_opens_mode_selection_without_default_mode(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.handlers as handlers
    from app.models import RouteCode

    monkeypatch.setattr(handlers, "is_private", lambda _callback: True)

    async def fake_prices() -> str:
        return "أسعار تجريبية"

    monkeypatch.setattr(handlers, "sp_today_home_text", fake_prices)
    prompt = SimpleNamespace(chat=SimpleNamespace(id=99), message_id=50)
    callback = SimpleNamespace(
        data=f"route:{RouteCode.USD_TO_USDT.value}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=99),
            delete=AsyncMock(),
            answer=AsyncMock(return_value=prompt),
        ),
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={}),
        get_state=AsyncMock(return_value=None),
        clear=AsyncMock(),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
    )

    await handlers.choose_route(callback, state)

    state.set_state.assert_awaited_once_with(handlers.UserFlow.awaiting_quote_mode)
    state.update_data.assert_any_await(
        route_code=RouteCode.USD_TO_USDT.value,
        quote_mode=None,
    )
    prompt_text = callback.message.answer.await_args.args[0]
    keyboard = callback.message.answer.await_args.kwargs["reply_markup"]
    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    }
    assert handlers.ROUTE_LABELS[RouteCode.USD_TO_USDT] in prompt_text
    assert {"quote:mode:receive", "quote:mode:send"}.issubset(callbacks)


@pytest.mark.asyncio
async def test_number_before_mode_choice_is_rejected_without_saving_amount() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.handlers as handlers
    from app.models import RouteCode

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=99),
        chat=SimpleNamespace(id=99),
        delete=AsyncMock(),
        answer=AsyncMock(),
        bot=SimpleNamespace(edit_message_text=AsyncMock()),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={
            "route_code": RouteCode.USD_TO_USDT.value,
            "prompt_chat_id": 99,
            "prompt_message_id": 50,
        }),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
    )

    await handlers.require_quote_mode_before_amount(message, state)

    message.delete.assert_awaited_once()
    message.answer.assert_not_awaited()
    text = message.bot.edit_message_text.await_args.kwargs["text"]
    assert "اختر طريقة الحساب" in text
    assert handlers.ROUTE_LABELS[RouteCode.USD_TO_USDT] in text
    callbacks = [
        button.callback_data
        for row in message.bot.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "quote:mode:receive" in callbacks
    assert "quote:mode:send" in callbacks
    state.update_data.assert_not_awaited()
    state.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_receive_target_amount_refuses_missing_quote_mode() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.handlers as handlers
    from app.models import RouteCode

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=99),
        text="100",
        chat=SimpleNamespace(id=99),
        bot=SimpleNamespace(edit_message_text=AsyncMock()),
        delete=AsyncMock(),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={
            "route_code": RouteCode.USD_TO_USDT.value,
            "quote_mode": None,
            "prompt_chat_id": 99,
            "prompt_message_id": 50,
        }),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
        clear=AsyncMock(),
    )

    await handlers.receive_target_amount(message, state, SimpleNamespace())

    message.delete.assert_awaited_once()
    message.bot.edit_message_text.assert_awaited_once()
    assert "اختر طريقة الحساب أولاً" in message.bot.edit_message_text.await_args.kwargs["text"]
    assert handlers.ROUTE_LABELS[RouteCode.USD_TO_USDT] in message.bot.edit_message_text.await_args.kwargs["text"]
    state.set_state.assert_awaited_once_with(handlers.UserFlow.awaiting_quote_mode)
    state.update_data.assert_awaited_once_with(
        quote_mode=None,
        target_amount_input=None,
        target_quote_ready=False,
    )


@pytest.mark.parametrize("mode, prompt_fragment", [
    ("receive", "المبلغ الصافي الذي تريد استلامه"),
    ("send", "المبلغ المتوفر لديك للإرسال"),
])
@pytest.mark.asyncio
async def test_selecting_quote_mode_opens_only_that_mode(monkeypatch, mode: str, prompt_fragment: str) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.handlers as handlers
    from app.models import RouteCode

    monkeypatch.setattr(handlers, "is_private", lambda _callback: True)
    callback = SimpleNamespace(
        data=f"quote:mode:{mode}",
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=99),
            message_id=50,
            edit_text=AsyncMock(),
        ),
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={
            "route_code": RouteCode.USD_TO_USDT.value,
            "quote_mode": None,
            "target_amount_input": None,
            "prompt_chat_id": 99,
            "prompt_message_id": 50,
        }),
        get_state=AsyncMock(return_value=handlers.UserFlow.awaiting_quote_mode.state),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
    )

    await handlers.switch_quote_mode(callback, state, SimpleNamespace())

    state.set_state.assert_awaited_once_with(handlers.UserFlow.awaiting_target_amount)
    state.update_data.assert_any_await(quote_mode=mode, target_quote_ready=False)
    prompt_text = callback.message.edit_text.await_args.args[0]
    assert handlers.ROUTE_LABELS[RouteCode.USD_TO_USDT] in prompt_text
    assert prompt_fragment in prompt_text


@pytest.mark.asyncio
async def test_postgresql_migration_adds_review_message_id() -> None:
    from types import SimpleNamespace
 
    statements: list[str] = []
 
    class FakeConnection:
        dialect = SimpleNamespace(name="postgresql")
 
        async def run_sync(self, _function):
            return None
 
        async def exec_driver_sql(self, statement: str):
            statements.append(statement)
 
    class FakeTransaction:
        async def __aenter__(self):
            return FakeConnection()
 
        async def __aexit__(self, *_args):
            return None
 
    class FakeEngine:
        def begin(self):
            return FakeTransaction()
 
    await initialize_database(FakeEngine())
    assert any(
        "ADD COLUMN IF NOT EXISTS review_message_id BIGINT" in statement
        for statement in statements
    )
    assert any(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_exchange_requests_payment_reference" in statement
        and "WHERE payment_reference IS NOT NULL" in statement
        for statement in statements
    )
 
 
@pytest.mark.asyncio
async def test_activity_guard_limits_callback_spam() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
 
    from app.middleware import ActivityGuard
 
    guard = ActivityGuard()
    callback = SimpleNamespace(answer=AsyncMock())
 
    for _ in range(5):
        assert not await guard._rate_limit_rejected(callback, None, 987654321)
 
    assert await guard._rate_limit_rejected(callback, None, 987654321)
    for _ in range(10):
        assert await guard._rate_limit_rejected(callback, None, 987654321)
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_sqlite_migration_restores_unique_payment_reference_index(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
    try:
        await initialize_database(engine)
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "DROP INDEX IF EXISTS uq_exchange_requests_payment_reference"
            )
        await initialize_database(engine)
        async with engine.connect() as connection:
            result = await connection.exec_driver_sql(
                "PRAGMA index_list('exchange_requests')"
            )
            indexes = result.fetchall()
        assert any(
            row[1] == "uq_exchange_requests_payment_reference" and row[2] == 1
            for row in indexes
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_user_review_callback_requires_current_state_and_message() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.handlers import UserFlow, is_current_user_review

    state = SimpleNamespace(get_state=AsyncMock(return_value=UserFlow.awaiting_user_review.state))
    callback = SimpleNamespace(
        message=SimpleNamespace(
            message_id=100,
            chat=SimpleNamespace(id=55),
        )
    )
    data = {"prompt_message_id": 100, "prompt_chat_id": 55}
    assert await is_current_user_review(callback, state, data)
    assert not await is_current_user_review(
        SimpleNamespace(message=SimpleNamespace(message_id=99, chat=SimpleNamespace(id=55))),
        state,
        data,
    )
    state.get_state.return_value = UserFlow.awaiting_reference.state
    assert not await is_current_user_review(callback, state, data)


@pytest.mark.asyncio
async def test_continue_quote_concurrency_creates_only_one_draft(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.handlers as handlers
    from app.models import Currency, RouteCode

    class FakeState:
        def __init__(self) -> None:
            self.data = {
                "target_amount_input": "100",
                "target_quote_ready": True,
                "route_code": RouteCode.USD_TO_SYP.value,
                "quote_mode": "receive",
                "prompt_chat_id": 55,
                "prompt_message_id": 101,
            }
            self.current = handlers.UserFlow.awaiting_target_amount.state

        async def get_data(self):
            return dict(self.data)

        async def update_data(self, **values):
            self.data.update(values)

        async def get_state(self):
            return self.current

        async def set_state(self, value):
            self.current = value.state

    class FakeService:
        def __init__(self) -> None:
            self.created = 0

        async def create_target_draft(self, **_kwargs):
            self.created += 1
            await asyncio.sleep(0)
            return SimpleNamespace(
                public_id="ORDER-1",
                route_code=RouteCode.USD_TO_SYP,
                from_amount=Decimal("101.01"),
                to_amount=Decimal("1400000"),
                from_currency=Currency.USD,
                to_currency=Currency.SYP,
                fee_percentage_snapshot=Decimal("1"),
                rate_snapshot=Decimal("14000"),
            )

    state = FakeState()
    service = FakeService()
    session = SimpleNamespace(close=AsyncMock())

    async def fake_service_session(_app_context):
        return service, session

    monkeypatch.setattr(handlers, "service_session", fake_service_session)
    monkeypatch.setattr(handlers, "is_private", lambda _update: True)
    chat = SimpleNamespace(id=55, type="private")
    message = SimpleNamespace(
        message_id=101,
        chat=chat,
        edit_text=AsyncMock(),
    )
    user = SimpleNamespace(id=77, username="tester", full_name="Test User")
    callbacks = [
        SimpleNamespace(
            message=message,
            from_user=user,
            answer=AsyncMock(),
        )
        for _ in range(2)
    ]

    await asyncio.gather(*(
        handlers.continue_with_target_quote(callback, state, object())
        for callback in callbacks
    ))
    assert service.created == 1
    assert state.current == handlers.UserFlow.awaiting_payout_address.state


@pytest.mark.asyncio
async def test_application_context_caches_and_invalidates_monitoring() -> None:
    from app.context import ApplicationContext

    settings = make_settings()
    service = MemoryService(settings)
    await service.bootstrap()
    context = ApplicationContext(settings=settings, memory_service=service)

    assert await context.monitoring_enabled_cached() is False
    await service.set_monitoring(True, 123456789)
    assert await context.monitoring_enabled_cached() is False
    context.cache_monitoring_enabled(True)
    assert await context.monitoring_enabled_cached() is True


def test_telegram_text_chunker_preserves_unicode_and_html_characters() -> None:
    from app.middleware import _split_by_utf16_units, _utf16_units

    original = "🙂<&>" * 1500
    chunks = _split_by_utf16_units(original, 1024)
    assert "".join(chunks) == original
    assert all(_utf16_units(chunk) <= 1024 for chunk in chunks)


@pytest.mark.asyncio
async def test_stale_withdrawal_action_opens_account_details_first(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.handlers as handlers

    request = SimpleNamespace(
        public_id="W-TEST-1",
        user_id=777,
        amount=Decimal("25.00"),
        method_snapshot="Test method",
        account_snapshot="FULL-DESTINATION-ACCOUNT-123",
    )

    class FakeService:
        async def pending_withdrawals(self):
            return [request]

    service = FakeService()
    session = SimpleNamespace(close=AsyncMock())
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={}),
        get_state=AsyncMock(return_value=None),
        set_state=AsyncMock(),
        update_data=AsyncMock(),
    )
    callback = SimpleNamespace(
        data="admin:withdrawal:approve:W-TEST-1",
        message=SimpleNamespace(
            message_id=303,
            text="قائمة السحوبات",
            edit_text=AsyncMock(),
        ),
        from_user=SimpleNamespace(id=123456789),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock())

    async def fake_service_session(_app_context):
        return service, session

    monkeypatch.setattr(handlers, "require_admin", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers, "service_session", fake_service_session)

    await handlers.admin_resolve_withdrawal(callback, state, object(), bot)

    callback.message.edit_text.assert_awaited_once()
    assert "FULL-DESTINATION-ACCOUNT-123" in callback.message.edit_text.await_args.args[0]
    state.set_state.assert_awaited_once_with(handlers.AdminFlow.awaiting_withdrawal_decision)
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_review_action_lock_serializes_concurrent_confirmation(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace

    import app.handlers as handlers

    class FakeState:
        current = "review"

    state = FakeState()
    calls = 0

    async def fake_locked(_callback, _state, _app_context, _bot):
        nonlocal calls
        if state.current != "review":
            return
        calls += 1
        state.current = "processing"
        await asyncio.sleep(0)
        state.current = "done"

    monkeypatch.setattr(handlers, "_confirm_user_request_locked", fake_locked)
    callback = SimpleNamespace(from_user=SimpleNamespace(id=880001))
    await asyncio.gather(*(
        handlers.confirm_user_request(callback, state, object(), object())
        for _ in range(2)
    ))
    assert calls == 1
    assert state.current == "done"


@pytest.mark.asyncio
async def test_receive_compare_amount_shows_enabled_route_quotes_without_creating_request(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.handlers as handlers
    from app.models import Currency, RouteCode

    routes = [
        SimpleNamespace(code=RouteCode.USD_TO_SYP, from_currency=Currency.USD, to_currency=Currency.SYP),
        SimpleNamespace(code=RouteCode.USD_TO_USDT, from_currency=Currency.USD, to_currency=Currency.USDT),
        SimpleNamespace(code=RouteCode.USDT_TO_SYP, from_currency=Currency.USDT, to_currency=Currency.SYP),
        SimpleNamespace(code=RouteCode.USDT_TO_USD, from_currency=Currency.USDT, to_currency=Currency.USD),
    ]

    class FakeService:
        async def enabled_routes(self):
            return routes

        async def current_rates(self):
            return SimpleNamespace(minimum_usd=Decimal("1"))

    service = FakeService()
    session = SimpleNamespace(close=AsyncMock())

    async def fake_service_session(_app_context):
        return service, session

    async def fake_live_rate(route_code, force_refresh=False):
        if route_code is RouteCode.USD_TO_SYP:
            return Decimal("141")
        if route_code is RouteCode.USDT_TO_SYP:
            return Decimal("142")
        return Decimal("1")

    monkeypatch.setattr(handlers, "is_private", lambda _message: True)
    monkeypatch.setattr(handlers, "service_session", fake_service_session)
    monkeypatch.setattr(handlers, "live_rate_for_route", fake_live_rate)

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=71),
        chat=SimpleNamespace(id=71),
        text="100",
        delete=AsyncMock(),
    )
    bot = SimpleNamespace(edit_message_text=AsyncMock())
    state_value = [handlers.UserFlow.awaiting_compare_amount.state]
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={
            "compare_category": "USD",
            "prompt_chat_id": 71,
            "prompt_message_id": 900,
        }),
        get_state=AsyncMock(side_effect=lambda: state_value[0]),
        set_state=AsyncMock(side_effect=lambda value: state_value.__setitem__(0, value)),
        update_data=AsyncMock(),
    )
    app_context = SimpleNamespace(settings=SimpleNamespace(global_fee_percentage=Decimal("1")))

    await handlers.receive_compare_amount(message, state, app_context, bot)

    message.delete.assert_awaited_once()
    session.close.assert_awaited_once()
    assert state.set_state.await_count == 2
    assert state_value[0] == handlers.UserFlow.awaiting_compare_amount.state
    output = bot.edit_message_text.await_args.kwargs["text"]
    assert "13,959 ل.س" in output
    assert "99 USDT" in output
    assert "14,058 ل.س" in output
    assert "إذا كان المبلغ شام كاش دولار" in output
    assert "إذا كان المبلغ USDT" in output
    assert "أرسل مبلغاً آخر للحساب." in output
    keyboard = bot.edit_message_text.await_args.kwargs["reply_markup"]
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert callbacks == ["compare:source:SYP", "compare:source:USD", "menu:home"]


@pytest.mark.asyncio
async def test_compare_menu_offers_exactly_two_currency_categories_and_back(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.handlers as handlers

    monkeypatch.setattr(handlers, "is_private", lambda _callback: True)
    monkeypatch.setattr(handlers, "prepare_calculator_navigation", AsyncMock(return_value=False))
    callback = SimpleNamespace(
        message=SimpleNamespace(
            chat=SimpleNamespace(id=71),
            message_id=900,
            edit_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(
        clear=AsyncMock(),
        set_state=AsyncMock(),
        update_data=AsyncMock(),
    )

    await handlers.compare_menu(callback, state, object())

    state.set_state.assert_awaited_once_with(handlers.UserFlow.awaiting_compare_currency)
    keyboard = callback.message.edit_text.await_args.kwargs["reply_markup"]
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert callbacks == ["compare:source:SYP", "compare:source:USD", "menu:home"]
    assert "إيجاد الصيغة المناسبة" in callback.message.edit_text.await_args.args[0]
    state.update_data.assert_awaited_once_with(
        prompt_chat_id=71,
        prompt_message_id=900,
    )


@pytest.mark.asyncio
async def test_main_calculator_opens_simple_modes_on_the_same_message(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.handlers as handlers

    monkeypatch.setattr(handlers, "is_private", lambda _callback: True)
    monkeypatch.setattr(handlers, "prepare_calculator_navigation", AsyncMock(return_value=False))
    callback = SimpleNamespace(
        message=SimpleNamespace(
            chat=SimpleNamespace(id=71),
            message_id=900,
            edit_text=AsyncMock(),
            delete=AsyncMock(),
            answer=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(
        clear=AsyncMock(),
        set_state=AsyncMock(),
        update_data=AsyncMock(),
    )

    await handlers.calculator_menu(callback, state, object())

    callback.message.edit_text.assert_awaited_once()
    callback.message.delete.assert_not_awaited()
    callback.message.answer.assert_not_awaited()
    assert "(ملاحظة: هذه الحاسبة مخصصة للمعرفة والاطلاع فقط)" in callback.message.edit_text.await_args.args[0]
    callbacks = [
        button.callback_data
        for row in callback.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert callbacks == ["quickcalc:mode:send", "quickcalc:mode:receive", "menu:home"]


@pytest.mark.asyncio
async def test_compare_source_updates_same_message_and_explains_usd_and_usdt(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.handlers as handlers
    from app.models import Currency, RouteCode

    routes = [
        SimpleNamespace(code=RouteCode.USD_TO_USDT, from_currency=Currency.USD),
        SimpleNamespace(code=RouteCode.USDT_TO_SYP, from_currency=Currency.USDT),
    ]

    class FakeService:
        async def enabled_routes(self):
            return routes

    session = SimpleNamespace(close=AsyncMock())

    async def fake_service_session(_app_context):
        return FakeService(), session

    async def fake_live_rate(route_code):
        return Decimal("1") if route_code is RouteCode.USD_TO_USDT else Decimal("142")

    monkeypatch.setattr(handlers, "is_private", lambda _callback: True)
    monkeypatch.setattr(handlers, "service_session", fake_service_session)
    monkeypatch.setattr(handlers, "live_rate_for_route", fake_live_rate)
    callback = SimpleNamespace(
        data="compare:source:USD",
        from_user=SimpleNamespace(id=71),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=71),
            message_id=900,
            edit_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    state_value = [handlers.UserFlow.awaiting_compare_currency.state]
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"prompt_chat_id": 71, "prompt_message_id": 900}),
        get_state=AsyncMock(side_effect=lambda: state_value[0]),
        set_state=AsyncMock(side_effect=lambda value: state_value.__setitem__(0, value)),
        update_data=AsyncMock(),
    )

    await handlers.choose_compare_source(callback, state, object())

    session.close.assert_awaited_once()
    assert state_value[0] == handlers.UserFlow.awaiting_compare_amount.state
    assert "اخترت: دولار (شام كاش و USDT)" in callback.message.edit_text.await_args.args[0]
    assert "الدولار≈USDT" in callback.message.edit_text.await_args.args[0]
    assert "أرسل المبلغ بالأرقام" in callback.message.edit_text.await_args.args[0]
    callbacks = [
        button.callback_data
        for row in callback.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert callbacks == ["compare:source:SYP", "compare:source:USD", "menu:home"]


@pytest.mark.asyncio
async def test_quick_calculator_displays_quote_on_same_message_without_request_creation(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.handlers as handlers
    from app.models import Currency, RouteCode

    route = SimpleNamespace(
        code=RouteCode.USD_TO_USDT,
        from_currency=Currency.USD,
        to_currency=Currency.USDT,
        is_enabled=True,
    )

    class FakeService:
        async def get_route(self, _route_code):
            return route

        async def current_rates(self):
            return SimpleNamespace(minimum_usd=Decimal("1"))

    session = SimpleNamespace(close=AsyncMock())

    async def fake_service_session(_app_context):
        return FakeService(), session

    async def fake_live_rate(_route_code):
        return Decimal("1")

    monkeypatch.setattr(handlers, "is_private", lambda _message: True)
    monkeypatch.setattr(handlers, "service_session", fake_service_session)
    monkeypatch.setattr(handlers, "live_rate_for_route", fake_live_rate)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=81),
        chat=SimpleNamespace(id=81),
        text="100",
        delete=AsyncMock(),
    )
    bot = SimpleNamespace(edit_message_text=AsyncMock())
    state_value = [handlers.UserFlow.awaiting_quick_calc_amount.state]
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={
            "quick_calc_route": RouteCode.USD_TO_USDT.value,
            "quick_calc_mode": "send",
            "prompt_chat_id": 81,
            "prompt_message_id": 901,
        }),
        get_state=AsyncMock(side_effect=lambda: state_value[0]),
        set_state=AsyncMock(side_effect=lambda value: state_value.__setitem__(0, value)),
        update_data=AsyncMock(),
    )
    app_context = SimpleNamespace(settings=SimpleNamespace(global_fee_percentage=Decimal("1")))

    await handlers.receive_quick_calculator_amount(message, state, app_context, bot)

    message.delete.assert_awaited_once()
    session.close.assert_awaited_once()
    text = bot.edit_message_text.await_args.kwargs["text"]
    assert "صافي الاستلام: <b>99 USDT</b>" in text
    assert "هذا التقرير يتناسب مع سعر الصرف الحالي." in text
    assert state_value[0] == handlers.UserFlow.awaiting_quick_calc_amount.state
    assert state.set_state.await_count == 2


@pytest.mark.asyncio
async def test_legacy_compare_result_button_cannot_enter_calculator_or_create_request() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.handlers as handlers

    callback = SimpleNamespace(
        data="compare:choose:USD_TO_USDT",
        answer=AsyncMock(),
    )

    await handlers.choose_compare_result(callback, object(), object())

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert "لا تنتقل منها إلى طلب" in callback.answer.await_args.args[0]


@pytest.mark.parametrize(
    ("mode", "expected_prompt"),
    [
        ("send", "المبلغ المتوفر لديك للإرسال"),
        ("receive", "صافي المبلغ الذي تريد استلامه"),
    ],
)
@pytest.mark.asyncio
async def test_edit_request_prompt_preserves_calculation_mode(
    monkeypatch,
    mode: str,
    expected_prompt: str,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.handlers as handlers
    from app.models import RouteCode

    service = SimpleNamespace(restart_draft_for_edit=AsyncMock())
    session = SimpleNamespace(close=AsyncMock())

    async def fake_service_session(_app_context):
        return service, session

    monkeypatch.setattr(handlers, "is_private", lambda _callback: True)
    monkeypatch.setattr(handlers, "is_current_user_review", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers, "service_session", fake_service_session)
    monkeypatch.setattr(handlers, "cancel_expiry_task", lambda _request_id: None)
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=99),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=99),
            delete=AsyncMock(),
            answer=AsyncMock(return_value=SimpleNamespace(
                chat=SimpleNamespace(id=99),
                message_id=901,
            )),
        ),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={
            "request_id": "REQ-1",
            "route_code": RouteCode.USD_TO_USDT.value,
            "quote_mode": mode,
        }),
        set_state=AsyncMock(),
        update_data=AsyncMock(),
    )

    await handlers.edit_user_request(callback, state, object())

    service.restart_draft_for_edit.assert_awaited_once_with("REQ-1", 99)
    session.close.assert_awaited_once()
    prompt = callback.message.answer.await_args.args[0]
    assert expected_prompt in prompt
    state.update_data.assert_any_await(
        request_id="REQ-1",
        editing_target=True,
        route_code=RouteCode.USD_TO_USDT.value,
        quote_mode=mode,
    )
    keyboard = callback.message.answer.await_args.kwargs["reply_markup"]
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert callbacks == ["flow:cancel"]


@pytest.mark.parametrize(
    ("mode", "expected_service_method"),
    [("send", "set_amount"), ("receive", "set_target_amount")],
)
@pytest.mark.asyncio
async def test_edit_request_uses_amount_service_for_selected_mode(
    monkeypatch,
    mode: str,
    expected_service_method: str,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.handlers as handlers
    from app.models import Currency, RouteCode

    request = SimpleNamespace(
        public_id="REQ-1",
        route_code=RouteCode.USD_TO_USDT,
        from_amount=Decimal("100"),
        to_amount=Decimal("99"),
        from_currency=Currency.USD,
        to_currency=Currency.USDT,
    )
    service = SimpleNamespace(
        set_amount=AsyncMock(return_value=request),
        set_target_amount=AsyncMock(return_value=request),
    )
    session = SimpleNamespace(close=AsyncMock())

    async def fake_service_session(_app_context):
        return service, session

    async def fake_prices():
        return "prices"

    monkeypatch.setattr(handlers, "service_session", fake_service_session)
    monkeypatch.setattr(handlers, "sp_today_home_text", fake_prices)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=99, username="customer", full_name="Customer"),
        chat=SimpleNamespace(id=99),
        text="100",
        delete=AsyncMock(),
        answer=AsyncMock(return_value=SimpleNamespace(
            chat=SimpleNamespace(id=99),
            message_id=902,
        )),
    )
    message.bot = SimpleNamespace(delete_message=AsyncMock())
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={
            "route_code": RouteCode.USD_TO_USDT.value,
            "quote_mode": mode,
            "editing_target": True,
            "request_id": "REQ-1",
            "prompt_chat_id": 99,
            "prompt_message_id": 901,
        }),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
    )

    await handlers.receive_target_amount(message, state, object())

    getattr(service, expected_service_method).assert_awaited_once_with(
        "REQ-1",
        99,
        "100",
    )
    other_method = "set_target_amount" if expected_service_method == "set_amount" else "set_amount"
    getattr(service, other_method).assert_not_awaited()
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_activity_guard_caches_only_positive_membership_checks() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.middleware import ActivityGuard

    bot = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member"))
    )
    guard = ActivityGuard()

    assert await guard._check_channel_membership(bot, "@channel", 123) == (True, True)
    assert await guard._check_channel_membership(bot, "@channel", 123) == (True, False)
    bot.get_chat_member.assert_awaited_once_with("@channel", 123)

    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="left"))
    assert await guard._check_channel_membership(bot, "@channel", 456) == (False, True)
    assert await guard._check_channel_membership(bot, "@channel", 456) == (False, True)
    assert bot.get_chat_member.await_count == 2
