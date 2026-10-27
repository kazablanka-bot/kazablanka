from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import PROJECT_ROOT, Settings, get_settings
from app.keyboards import (
    admin_home_menu,
    comparison_source_menu,
    main_menu,
    quick_calculator_mode_menu,
    quick_calculator_result_menu,
    quick_calculator_routes_menu,
)
from app.models import Currency, RouteCode

FAKE_TOKEN = "123456:TEST_TOKEN_DO_NOT_USE"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123456789", [123456789]),
        ("[123456789, 987654321]", [123456789, 987654321]),
        ("123456789,987654321", [123456789, 987654321]),
    ],
)
def test_admin_ids_from_environment(monkeypatch, raw, expected):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("ADMIN_IDS", raw)
    monkeypatch.setenv("STORAGE_MODE", "memory")
    monkeypatch.setenv("DEFAULT_SYP_PER_USD", "10000")
    monkeypatch.setenv("DEFAULT_USDT_PER_USD", "1")
    monkeypatch.setenv("MINIMUM_USD", "50")
    monkeypatch.setenv("DEFAULT_USD_TO_USDT_FEE", "0")
    monkeypatch.setenv("DEFAULT_USDT_TO_USD_FEE", "0")
    settings = Settings(_env_file=None)
    assert settings.admin_ids == expected


def test_env_file_is_project_relative():
    assert Path(Settings.model_config["env_file"]) == PROJECT_ROOT / ".env"
    assert Path(Settings.model_config["env_file"]).is_absolute()


def test_explicit_env_file_and_environment_priority(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\nADMIN_IDS=123456789,987654321\n"
        "STORAGE_MODE=memory\nDEFAULT_SYP_PER_USD=10000\nDEFAULT_USDT_PER_USD=1\nMINIMUM_USD=50\nDEFAULT_USD_TO_USDT_FEE=0\nDEFAULT_USDT_TO_USD_FEE=0\n",
        encoding="utf-8",
    )
    for key in ("TELEGRAM_BOT_TOKEN", "ADMIN_IDS", "STORAGE_MODE", "DEFAULT_SYP_PER_USD", "DEFAULT_USDT_PER_USD", "MINIMUM_USD", "DEFAULT_USD_TO_USDT_FEE", "DEFAULT_USDT_TO_USD_FEE"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=env_file)
    assert settings.admin_ids == [123456789, 987654321]
    monkeypatch.setenv("ADMIN_IDS", "[999999999]")
    assert Settings(_env_file=env_file).admin_ids == [999999999]


def test_root_entrypoint_imports_without_starting_polling():
    entrypoint = importlib.import_module("main")
    app_main = importlib.import_module("app.main")
    assert entrypoint.main is app_main.main


def test_entrypoint_imports_from_other_working_directory(tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", "import main; import app.config; print('import-ok')"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "import-ok" in result.stdout


@pytest.mark.asyncio
async def test_memory_startup_with_mocked_telegram(monkeypatch):
    app_main = importlib.import_module("app.main")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("ADMIN_IDS", "123456789,987654321")
    monkeypatch.setenv("STORAGE_MODE", "memory")
    monkeypatch.setenv("DEFAULT_SYP_PER_USD", "10000")
    monkeypatch.setenv("DEFAULT_USDT_PER_USD", "1")
    monkeypatch.setenv("MINIMUM_USD", "50")
    monkeypatch.setenv("DEFAULT_USD_TO_USDT_FEE", "0")
    monkeypatch.setenv("DEFAULT_USDT_TO_USD_FEE", "0")
    get_settings.cache_clear()

    bot = MagicMock()
    bot.delete_webhook = AsyncMock()
    bot.set_my_commands = AsyncMock()
    bot.session.close = AsyncMock()
    bot_factory = MagicMock(return_value=bot)

    dispatcher = MagicMock()
    dispatcher.resolve_used_update_types.return_value = ["message", "callback_query"]
    dispatcher.start_polling = AsyncMock()
    dispatcher_factory = MagicMock(return_value=dispatcher)

    monkeypatch.setattr(app_main, "Bot", bot_factory)
    monkeypatch.setattr(app_main, "Dispatcher", dispatcher_factory)
    try:
        await app_main.main()
        bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
        assert bot.set_my_commands.await_count == 3
        dispatcher.start_polling.assert_awaited_once()
        bot.session.close.assert_awaited_once()
        assert dispatcher_factory.call_args.kwargs["app_context"].memory_service is not None
    finally:
        get_settings.cache_clear()


def test_asyncio_sqlalchemy_dependency_is_available():
    import greenlet
    from sqlalchemy.ext.asyncio import AsyncSession

    assert greenlet.greenlet is not None
    assert AsyncSession is not None


def test_user_home_exposes_calculator_and_existing_actions():
    keyboard = main_menu(False, "https://example.test/contact")
    buttons = [button for row in keyboard.inline_keyboard for button in row]

    assert len(buttons) == 6
    assert [button.callback_data for button in buttons[:5]] == [
        "menu:convert",     # الزر الأول (إنشاء طلب تحويل)
        "menu:compare",     # الزر الثاني (قارن وعاين)
        "menu:calculator",  # الزر الثالث (احسب هنا)
        "menu:referrals",   # الزر الرابع (الربح والكسب)
        "menu:requests",    # الزر الخامس (سجل طلباتي)
    ]
    assert len(keyboard.inline_keyboard[0]) == 1
    assert keyboard.inline_keyboard[0][0].callback_data == "menu:convert"     # الصف الأول، الزر الأول
    assert keyboard.inline_keyboard[1][0].callback_data == "menu:compare"     # الصف الثاني، الزر الأول
    assert keyboard.inline_keyboard[1][1].callback_data == "menu:calculator"  # الصف الثاني، الزر الثاني


def test_comparison_keyboard_has_two_currency_choices_and_back_only():
    keyboard = comparison_source_menu()
    buttons = [button for row in keyboard.inline_keyboard for button in row]

    assert [button.text for button in buttons] == [
        "ليرة سورية (شام كاش)",
        "دولار (شام كاش و USDT)",
        "↪️ العودة إلى القائمة الرئيسية ↩️",
    ]
    assert [button.callback_data for button in buttons] == [
        "compare:source:SYP",
        "compare:source:USD",
        "menu:home",
    ]


def test_calculator_menu_callback_is_registered():
    import app.handlers as handlers

    callback_names = {
        handler.callback.__name__
        for handler in handlers.router.callback_query.handlers
    }
    assert "calculator_menu" in callback_names
    assert "compare_menu" in callback_names
    assert "choose_compare_source" in callback_names
    assert "choose_compare_result" in callback_names
    assert "choose_quick_calculator_mode" in callback_names
    assert "choose_quick_calculator_route" in callback_names
    message_names = {
        handler.callback.__name__
        for handler in handlers.router.message.handlers
    }
    assert "receive_quick_calculator_amount" in message_names


def test_quick_calculator_keyboards_are_short_and_do_not_create_requests():
    mode_callbacks = [
        button.callback_data
        for row in quick_calculator_mode_menu().inline_keyboard
        for button in row
    ]
    assert mode_callbacks == [
        "quickcalc:mode:send",
        "quickcalc:mode:receive",
        "menu:home",
    ]

    route = type("Route", (), {"code": RouteCode.USD_TO_USDT})()
    route_callbacks = [
        button.callback_data
        for row in quick_calculator_routes_menu([route]).inline_keyboard
        for button in row
    ]
    assert route_callbacks == ["quickcalc:route:USD_TO_USDT", "quickcalc:change-mode"]

    result_callbacks = [
        button.callback_data
        for row in quick_calculator_result_menu().inline_keyboard
        for button in row
    ]
    assert result_callbacks == [
        "quickcalc:change-route",
        "quickcalc:change-mode",
        "menu:home",
    ]


def test_admin_monitoring_label_is_neutral_when_state_is_unknown():
    keyboard = admin_home_menu()
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "تبديل وضع المتابعة" in labels
    assert "تفعيل وضع المتابعة" not in labels
def test_admin_error_alert_hides_raw_exception_details() -> None:
    import logging
 
    from app.main import TelegramAdminLogHandler
 
    class ImmediateLoop:
        def call_soon_threadsafe(self, callback, *args) -> None:
            callback(*args)
 
    handler = TelegramAdminLogHandler(
        bot=MagicMock(),
        admin_ids=[123456789],
        loop=ImmediateLoop(),
    )
    record = logging.LogRecord(
        name="app.handlers",
        level=logging.ERROR,
        pathname="handlers.py",
        lineno=123,
        msg="payment failed for %s",
        args=("wallet-secret-value",),
        exc_info=(ValueError, ValueError("private-address-secret"), None),
    )
    record.funcName = "receive_payment_proof"
 
    handler.emit(record)
    notification = handler.queue.get_nowait()
 
    assert "#000001" in notification
    assert "E-" in notification
    assert "ValueError" in notification
    assert "receive_payment_proof" in notification
    assert "wallet-secret-value" not in notification
    assert "private-address-secret" not in notification
