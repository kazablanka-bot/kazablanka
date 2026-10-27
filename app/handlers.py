"""معالجات تيليغرام للعميل ولوحة التحكم الشاملة."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
import html
import io
import logging
from pathlib import Path
import re
from typing import Final
import weakref

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from decimal import Decimal, ROUND_DOWN
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ChatMemberUpdated, CallbackQuery, ErrorEvent, FSInputFile, Message
from aiogram.utils.markdown import hcode
from decimal import Decimal

from app.context import ApplicationContext
from app.keyboards import (
    ROUTE_LABELS,
    admin_home_menu,
    admin_payment_waiting_menu,
    admin_edit_menu,
    request_confirmation_menu,
    admin_request_list_menu,
    CRYPTO_METHOD_LABELS,
    crypto_methods_menu,
    crypto_method_detail_menu,
    user_crypto_method_menu,
    admin_withdrawal_menu,
    admin_withdrawal_list_menu,
    admin_referrals_menu,
    broadcast_menu,
    confirm_broadcast_menu,
    monitoring_reply_menu,
    referrals_menu,
    user_management_menu,
    cancel_menu,
    target_calculator_menu,
    comparison_source_menu,
    quick_calculator_mode_menu,
    quick_calculator_routes_menu,
    quick_calculator_result_menu,
    main_menu,
    payment_menu,
    rates_menu,
    rates_refresh_menu,
    review_menu,
    receive_currency_menu,
    send_currency_menu,
    subscription_menu,
    start_only_menu,
    route_detail_menu,
    route_settings_menu,
    wallet_menu,
    withdrawal_confirm_menu,
    withdrawal_method_menu,
)
from app.models import Currency, ExchangeRequest, ExchangeRoute, RequestStatus, RouteCode
from app.services import (
    DomainError,
    get_sp_today_usd_rates,
    DuplicatePaymentReferenceError,
    ExchangeRates,
    ExchangeService,
    quantum_for,
    calculate_target_breakdown,
    ensure_utc,
    format_amount,
    format_rate,
    calculate_payment_for_target,
    decimal_from_text,
    live_rate_for_route,
    calculate_route_quote,
    minimum_transfer,
    referral_level_for_count,
    referral_reward_for_level,
)

logger = logging.getLogger(__name__)
router = Router(name="exchange")
_EXPIRY_TASKS: set[asyncio.Task[None]] = set()
_EXPIRY_TASKS_BY_REQUEST: dict[str, asyncio.Task[None]] = {}
_BROADCAST_LOCKS: dict[int, asyncio.Lock] = {}
_TARGET_QUOTE_LOCKS: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
_USER_REVIEW_LOCKS: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
_WITHDRAWAL_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_REQUEST_RESOLUTION_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_COMPARISON_LOCKS: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
_QUICK_CALC_LOCKS: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()

MAX_PAYMENT_PROOF_BYTES: Final[int] = 20 * 1024 * 1024


def payment_proof_file_size_allowed(file_size: int | None) -> bool:
    return file_size is not None and 0 < file_size <= MAX_PAYMENT_PROOF_BYTES


def payment_proof_pdf_signature_valid(content: bytes) -> bool:
    if len(content) < 16 or not content.startswith(b"%PDF-"):
        return False
    eof_offset = content.rfind(b"%%EOF")
    if eof_offset < 0 or content[eof_offset + len(b"%%EOF"):].strip():
        return False
    startxref_offset = content.rfind(b"startxref", 0, eof_offset)
    if startxref_offset < 0:
        return False
    xref_value = content[startxref_offset + len(b"startxref"):eof_offset].strip()
    if not xref_value.isdigit():
        return False
    xref_offset = int(xref_value)
    if xref_offset <= 0 or xref_offset >= startxref_offset:
        return False
    xref_section = content[xref_offset:min(startxref_offset, xref_offset + 4096)]
    if xref_section.startswith(b"xref"):
        return True
    return bool(
        re.match(rb"\d+\s+\d+\s+obj\b", xref_section)
        and b"/Type" in xref_section
        and b"/XRef" in xref_section
    )

RATE_LABELS: Final[dict[str, str]] = {
    "syp_per_usd": "السعر الابتدائي لليرة السورية الجديدة مقابل الدولار",
    "usdt_per_usd": "السعر الابتدائي لـ USDT مقابل الدولار",
    "minimum_usd": "الحد الأدنى العام بالدولار",
}
STATUS_LABELS: Final[dict[RequestStatus, str]] = {
    RequestStatus.AWAITING_REFERENCE: "بانتظار رقم العملية",
    RequestStatus.AWAITING_AMOUNT: "بانتظار القيمة",
    RequestStatus.AWAITING_PAYOUT_ADDRESS: "بانتظار عنوان الاستلام",
    RequestStatus.AWAITING_USER_REVIEW: "بانتظار تأكيد العميل",
    RequestStatus.PENDING_PAYMENT: "بانتظار الدفع",
    RequestStatus.PENDING_VERIFICATION: "تم الإبلاغ عن الدفع — بانتظار التحقق",
    RequestStatus.PENDING_REVIEW: "قيد المعالجة",
    RequestStatus.COMPLETED: "مكتملة",
    RequestStatus.REJECTED: "مرفوضة",
    RequestStatus.CANCELLED: "ملغاة",
    RequestStatus.EXPIRED: "منتهية الصلاحية",
}


class UserFlow(StatesGroup):
    awaiting_quote_mode = State()
    awaiting_target_amount = State()
    awaiting_compare_currency = State()
    awaiting_compare_amount = State()
    awaiting_compare_processing = State()
    awaiting_compare_results = State()
    awaiting_quick_calc_mode = State()
    awaiting_quick_calc_routes = State()
    awaiting_quick_calc_amount = State()
    quick_calc_processing = State()
    awaiting_reference = State()
    awaiting_amount = State()
    awaiting_payout_address = State()
    awaiting_crypto_method = State()
    awaiting_user_review = State()
    awaiting_withdrawal_account = State()
    awaiting_withdrawal_amount = State()

class AdminFlow(StatesGroup):
    awaiting_withdrawal_decision = State()
    awaiting_rate = State()
    awaiting_route_address = State()
    awaiting_route_photo = State()
    awaiting_ban_id = State()
    awaiting_direct_user_id = State()
    awaiting_broadcast_content = State()
    awaiting_monitor_reply = State()


def is_private(update: Message | CallbackQuery) -> bool:
    if isinstance(update, CallbackQuery):
        return bool(update.message and update.message.chat.type == ChatType.PRIVATE)
    return update.chat.type == ChatType.PRIVATE


def request_route_line(route_code: RouteCode) -> str:
    return (
        f"📝<b>الـطـلـب:</b>\n"
        f"<b>{html.escape(ROUTE_LABELS[route_code])}</b>\n\n"
    )


async def block_navigation_during_active_request(
    callback: CallbackQuery,
    state: FSMContext,
) -> bool:
    data = await state.get_data()
    if not isinstance(data.get("request_id"), str):
        return False
    try:
        route_line = request_route_line(RouteCode(str(data.get("route_code"))))
    except ValueError:
        route_line = ""
    await callback.answer(
        f"{route_line}أكمل الطلب الحالي أو ألغِه قبل بدء عملية أخرى.",
        show_alert=True,
    )
    return True


async def prepare_calculator_navigation(
    callback: CallbackQuery,
    state: FSMContext,
) -> bool:
    if await block_navigation_during_active_request(callback, state):
        return True
    current_state = await state.get_state()
    quote_states = {
        UserFlow.awaiting_quote_mode.state,
        UserFlow.awaiting_target_amount.state,
        UserFlow.awaiting_compare_currency.state,
        UserFlow.awaiting_compare_amount.state,
        UserFlow.awaiting_compare_processing.state,
        UserFlow.awaiting_compare_results.state,
        UserFlow.awaiting_quick_calc_mode.state,
        UserFlow.awaiting_quick_calc_routes.state,
        UserFlow.awaiting_quick_calc_amount.state,
        UserFlow.quick_calc_processing.state,
    }
    if current_state in quote_states:
        await state.clear()
        return False
    if current_state is not None:
        await callback.answer("أكمل الخطوة الحالية أو ألغِها قبل بدء عملية أخرى.", show_alert=True)
        return True
    return False


async def is_current_user_review(
    callback: CallbackQuery,
    state: FSMContext,
    data: dict[str, object],
) -> bool:
    return bool(
        callback.message
        and await state.get_state() == UserFlow.awaiting_user_review.state
        and callback.message.message_id == data.get("prompt_message_id")
        and callback.message.chat.id == data.get("prompt_chat_id")
    )


async def service_session(app_context: ApplicationContext) -> tuple[ExchangeService | object, object]:
    session = app_context.open_session()
    return app_context.service(session), session

def track_expiry_task(
    request_id: str,
    task: asyncio.Task[None],
) -> None:
    _EXPIRY_TASKS.add(task)
    _EXPIRY_TASKS_BY_REQUEST[request_id] = task

    def remove_expiry_task(completed_task: asyncio.Task[None]) -> None:
        _EXPIRY_TASKS.discard(completed_task)
        if _EXPIRY_TASKS_BY_REQUEST.get(request_id) is completed_task:
            _EXPIRY_TASKS_BY_REQUEST.pop(request_id, None)

    task.add_done_callback(remove_expiry_task)

def cancel_expiry_task(request_id: str) -> None:
    task = _EXPIRY_TASKS_BY_REQUEST.pop(request_id, None)
    if task is not None and not task.done():
        task.cancel()

def broadcast_lock(admin_id: int) -> asyncio.Lock:
    return _BROADCAST_LOCKS.setdefault(admin_id, asyncio.Lock())


async def copy_message_with_retry(
    bot: Bot, recipient: int, source_chat: int, source_message: int
) -> None:
    for attempt in range(3):
        try:
            await bot.copy_message(recipient, source_chat, source_message)
            return
        except TelegramRetryAfter as error:
            if attempt == 2:
                raise
            await asyncio.sleep(min(float(error.retry_after), 60.0))

    raise RuntimeError("حاول لاحقاً.")


async def expire_reference_barcode(
    bot: Bot,
    app_context: ApplicationContext,
    state: FSMContext,
    user_id: int,
    chat_id: int,
    request_id: str,
    barcode_message_id: int,
    expires_at: datetime,
) -> None:
    try:
        seconds_left = max(
            0.0,
            (ensure_utc(expires_at) - datetime.now(UTC)).total_seconds(),
        )
        await asyncio.sleep(seconds_left)

        service, session = await service_session(app_context)
        try:
            expired = await service.expire_reference_if_due(request_id, user_id)
        finally:
            await session.close()

        if not expired:
            return

        current_state = await state.get_state()
        state_data = await state.get_data()

        same_request_is_waiting_for_reference = (
            state_data.get("request_id") == request_id
            and current_state == UserFlow.awaiting_reference.state
        )

        if same_request_is_waiting_for_reference:
            await state.clear()

        try:
            await bot.delete_message(
                chat_id=chat_id,
                message_id=barcode_message_id,
            )
        except TelegramAPIError:
            logger.info("حاول لاحقاً. %s.", request_id)

        if same_request_is_waiting_for_reference:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "<b>🚨 انـتـهـت صـلاحـيـة بـيـانـات الـدفـع الـخـاصـة بـطـلـبـك الـسـابـق.</b>\n\n"
                    "<b>يـمـكـنـك إعـادة إنـشـاء طـلـب جـديـد فـي أي وقـت.</b>\n\n\n"
                    "<b>اضـعـط عـلـى الـزر أدنـاه لـلـمـتـابـعـة.</b>\n\n"
                ),
                reply_markup=start_only_menu()
            )

        else:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "<b>🚨 انـتـهـت صـلاحـيـة بـيـانـات الـدفـع الـخـاصـة بـطـلـبـك الـسـابـق.</b>\n\n"
                    "<b>يـمـكـنـك إعـادة إنـشـاء طـلـب جـديـد فـي أي وقـت.</b>\n\n\n"
                    "<b>اضـعـط عـلـى الـزر أدنـاه لـلـمـتـابـعـة.</b>\n\n"
                ),
                reply_markup=start_only_menu()
            )

    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "حاول لاحقاً. %s.",
            request_id,
        )

async def expire_user_review_message(
    bot: Bot,
    app_context: ApplicationContext,
    state: FSMContext,
    user_id: int,
    chat_id: int,
    request_id: str,
    review_message_id: int,
    expires_at: datetime,
) -> None:
    try:
        seconds_left = max(
            0.0,
            (ensure_utc(expires_at) - datetime.now(UTC)).total_seconds(),
        )
        await asyncio.sleep(seconds_left)

        service, session = await service_session(app_context)
        try:
            expired = await service.expire_user_review_if_due(request_id, user_id)
        finally:
            await session.close()

        if not expired:
            return

        state_data = await state.get_data()
        same_request_is_in_review = (
            state_data.get("request_id") == request_id
            and await state.get_state() == UserFlow.awaiting_user_review.state
        )
        if same_request_is_in_review:
            await state.clear()

        try:
            await bot.delete_message(chat_id=chat_id, message_id=review_message_id)
        except TelegramAPIError:
            logger.info("حاول لاحقاً %s.", request_id)

        await bot.send_message(
                chat_id=chat_id,
                text=(
                    "<b>🚨 انـتـهـت صـلاحـيـة عـرض الـسـعـر لـطـلـبـك الـسـابـق.\n\n</b>"
                    f"<b>رقـم الـطـلـب:</b> #{request_id} .\n\n"
                    "<b>يـمـكـنـك إعـادة إنـشـاء طـلـب جـديـد فـي أي وقـت.</b>\n\n\n"
                    "<b>اضـغـط عـلـى الـزر أدنـاه لـلـمـتـابـعـة.</b>\n\n"
                ),
                reply_markup=start_only_menu(),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("حدث خطأ أثناء إنهاء صلاحية مراجعة الطلب %s.", request_id)

async def restore_payment_expiry_tasks(
    bot: Bot,
    app_context: ApplicationContext,
    dispatcher: Dispatcher,
) -> None:
    service, session = await service_session(app_context)
    try:
        requests = await service.pending_requests(limit=10_000)
        review_requests = await service.pending_user_review_requests(limit=10_000)
    finally:
        await session.close()

    for request in requests:
        if request.status is not RequestStatus.PENDING_PAYMENT:
            continue
        if request.expires_at is None or request.payment_message_id is None:
            continue
        if request.public_id in _EXPIRY_TASKS_BY_REQUEST:
            continue

        state = dispatcher.fsm.get_context(
            bot=bot,
            chat_id=request.chat_id,
            user_id=request.user_id,
        )

        task = asyncio.create_task(
            expire_reference_barcode(
                bot=bot,
                app_context=app_context,
                state=state,
                user_id=request.user_id,
                chat_id=request.chat_id,
                request_id=request.public_id,
                barcode_message_id=request.payment_message_id,
                expires_at=request.expires_at,
            )
        )
        track_expiry_task(request.public_id, task)

    for request in review_requests:
        if request.expires_at is None or request.review_message_id is None:
            continue
        if request.public_id in _EXPIRY_TASKS_BY_REQUEST:
            continue

        state = dispatcher.fsm.get_context(
            bot=bot,
            chat_id=request.chat_id,
            user_id=request.user_id,
        )
        task = asyncio.create_task(
            expire_user_review_message(
                bot=bot,
                app_context=app_context,
                state=state,
                user_id=request.user_id,
                chat_id=request.chat_id,
                request_id=request.public_id,
                review_message_id=request.review_message_id,
                expires_at=request.expires_at,
            )
        )
        track_expiry_task(request.public_id, task)

def display_percentage(value) -> str:
    return format(value.normalize(), "f")

def rates_text(rates: ExchangeRates) -> str:
    return (
        "<b>سعر الصرف الحالي</b>\n"
        f"• كل 1 دولار = <b>{format_amount(rates.syp_per_usd, Currency.SYP)}</b>\n"
        f"• كل 1 دولار = <b>{format_amount(rates.usdt_per_usd, Currency.USDT)}</b>\n"
        f"• الحد الأدنى العام: <b>{format_amount(rates.minimum_usd, Currency.USD)}</b>\n"
        f"• مدة إنجاز العملية: <b>{html.escape(rates.completion_eta)}</b>"
    )


def request_summary(request: ExchangeRequest, show_sensitive: bool = False) -> str:
    lines = [
        f"<b>طلب #{hcode(request.public_id)}</b>",
        f"المسار: <b>{ROUTE_LABELS[request.route_code]}</b>",
        f"الحالة: <b>{STATUS_LABELS[request.status]}</b>",
    ]
    if request.from_amount is not None:
        lines.append(f"المحوّل: <b>{format_amount(request.from_amount, request.from_currency)}</b>")
    if request.to_amount is not None:
        lines.append(f"المستلم: <b>{format_amount(request.to_amount, request.to_currency)}</b>")
    if show_sensitive:
        lines.extend(
            [
                f"رقم العملية: {hcode(request.payment_reference or '—')}",
                f"عنوان / كود الاستلام: {hcode(request.payout_address or '—')}",
            ]
        )
    return "\n".join(lines)


def admin_request_summary(request: ExchangeRequest) -> str:
    customer = html.escape(request.full_name)
    if request.username:
        customer += f" (@{html.escape(request.username)})"

    crypto_method_line = ""
    if request.crypto_method in CRYPTO_METHOD_LABELS:
        crypto_method_line = (
            f"طريقة USDT: "
            f"<b>{html.escape(CRYPTO_METHOD_LABELS[request.crypto_method])}</b>\n"
        )

    if request.payment_proof_type == "photo":
        proof_label = "صورة مرفقة"
    elif request.payment_proof_type == "pdf":
        proof_label = "ملف PDF مرفق"
    else:
        proof_label = "—"

    route_details = (
        f"عمولة الخدمة المثبتة: "
        f"{display_percentage(request.fee_percentage_snapshot)}% | "
        f"الحد الأدنى: {request.minimum_usd_snapshot} دولار"
        if request.route_code in {RouteCode.USD_TO_USDT, RouteCode.USDT_TO_USD}
        else (
            f"الأسعار الحيّة: "
            f"{format_rate(request.rate_snapshot)} ليرة سورية جديدة | "
            f"الحد الأدنى: {request.minimum_usd_snapshot} دولار"
        )
    )

    return (
        f"<b>عملية تحويل جديدة #{hcode(request.public_id)}</b>\n"
        f"الحالة: <b>{STATUS_LABELS[request.status]}</b>\n"
        f"العميل: <b>{customer}</b>\n"
        f"معرف المستخدم: {hcode(str(request.user_id))}\n"
        f"المسار: <b>{ROUTE_LABELS[request.route_code]}</b>\n"
        f"{crypto_method_line}"
        f"رقم العملية: {hcode(request.payment_reference or '—')}\n"
        f"إثبات الدفع: <b>{proof_label}</b>\n"
        f"المبلغ المرسل: "
        f"<b>{format_amount(request.from_amount or 0, request.from_currency)}</b>\n"
        f"المبلغ المستلم: "
        f"<b>{format_amount(request.to_amount or 0, request.to_currency)}</b>\n"
        f"عنوان / كود استلام العميل: {hcode(request.payout_address or '—')}\n"
        f"مدة الإنجاز المعلنة: <b>{html.escape(request.eta_snapshot)}</b>\n"
        f"{route_details}"
    )

async def sp_today_home_text(force_refresh: bool = False) -> str:
    buy_syp, sell_syp, fetched_at = await get_sp_today_usd_rates(
        force_refresh=force_refresh
    )
    local_time = fetched_at.astimezone(
        ZoneInfo("Asia/Damascus")
    ).strftime("%H:%M")
    
    divider = "\u200F" + ("═" * 23)
        
    return (
        f"<b>{divider}</b>\n"
        "<b>📊 أســــعــــار الــــصــــرف | دولار ⇄ ل.س</b>\n\n"
        f"🔺 <b>مـبــيـع : </b> <b>{format_rate(sell_syp)} ل.س. </b>\n"
        f"🔻 <b>شــراء : </b> <b>{format_rate(buy_syp)} ل.س. </b>\n"
        "                                        <i>     (من الليرة اليوم)</i>\n"
        "🕒 <b><i>آخـر تـحـديـث: </i></b>"
        f"<i>{local_time} بـتـوقـيـت سـوريـا.</i>\n"
        f"<b>{divider}</b>\n"
    )
    
    
def build_home_text(prices_text: str) -> str:
    return (
		"<blockquote expandable>\n"
		"<b>🔴 🔘 إضــــغــــط هــــنــــا 🔘 🔴</b>\n\n"
		"<tg-spoiler>"
		"<b>✅ الـوسـاطـة الـرقـمـيـة الـسـوريـة ✅</b>\n"
		"<b>👋🏼 مــرحــبــاً بــك 👋🏼</b>\n"
		"<b>🥇 المنصة الأولى في سوريا  🥇</b> \n"
		"الأسرع والأكثر أماناً للتحويل الآلي الفوري.\n\n\n"
		"<b>▫️ خدماتنا 💸</b>\n"
		"تحويل فوري من وإلى:\n"
		"<b>شام كاش (ل.س/دولار) 🔁 USDT.</b>\n\n"
		"<b>▫️ مميزاتنا ⚡</b>\n"
		"مرونة تامة، سرعة تنفيذ عالية؛\n"
		"وخدمة 24/24 على مدار الساعة.\n\n"
		"ـ<b>━━━━━━━━━━━━━━━━━━</b>\n\n"
		"<b>▫️ نظام التشغيل والأمان 🤖</b>\n\n"
		"<b>▫️ نظام مؤتمت بالكامل ⚙️</b>\n"
		"جميع العمليات عبر هذا البوت آلية، تلقائية، ولحظية.\n"
		"تُنفّذ عبر خوادم سحابية مشفرة ومحمية لضمان السرعة والدقة.\n\n"
		"<b>▫️ حماية الخصوصية 🔒</b>\n"
		"<b>بياناتك ومعاملاتك في أمان تام وسرية مطلقة.</b>\n\n"
		"<b>▫️ الدعم الفني 🛠️</b>\n"
		"<b>في حال مواجهة أي مشكلة أو تأخير في وصول الحوالة (إن حصل)،</b>\n"
		"<b>يرجى نسخ رقم الطلب الخاص بك والتواصل مع الإدارة فوراً ليتم معالجة الموضوع بشكل فوري.</b>\n\n"
		"ـ<b>━━━━━━━━━━━━━━━━━━</b>\n\n"
		"<b>📊 نشرة سـعـر الـدولار اليومية 📊</b>\n\n"
		"<b>🔄 يتم تحديث الأسعار بشكل تلقائي ومستمر.</b>\n\n"
		"<b>👇🏼قبل الاعتماد على سعر البوت 👇🏼</b>\n\n"
		"<b>● يرجى التأكد دائماً من أسعار الصرف؛</b>\n"
		"<b>● وإذا كان هناك أي مشكلة او اختلاف بالسعر؛</b>\n"
		"<b>● فتواصل معنا بشكل فوري؛</b>\n"
		"<b>● ليتم معالجة الأمر وإتمام طلبك.</b>\n"
		"</tg-spoiler>\n"
		"</blockquote>\n"
		"● يسعدنا مساعدتك في التخطيط لطلبك بدقة.\n"
		"● قبل إنشاء الطلب، يمكنك استكشاف وحساب المبالغ التي تناسبك عبر الضغط على: \n"
		"<b>(قارن وعاين) أو (احسب هنا)،</b>\n"
		"● حيث تظهر لك جميع القيم الملائمة لميزانيتك فوراً وبشكل تلقائي.\n"
		f"{prices_text}"
		"🔄 لتحديث السعر، إضغط  <b>♻️تحديث الأسعار♻️</b>"
	)

async def show_home(message: Message, app_context: ApplicationContext, state: FSMContext | None = None) -> None:
    if state:
        await state.clear()
    is_admin = bool(message.from_user and app_context.is_admin(message.from_user.id))
    if is_admin:
        await message.answer("<b>لوحة التحكم</b>", reply_markup=admin_home_menu())
        return
    try:
        prices_text = await sp_today_home_text()
    except DomainError:
        prices_text = (
            "<b>تعذر جلب الأسعار حالياً.</b>\n"
            "حاول تحديثها بعد قليل."
        )
    home_text = build_home_text(prices_text)

 
    await message.answer(
        text=home_text,
        reply_markup=main_menu(
            is_admin,
            app_context.settings.contact_url,
            include_rates_refresh=True,
        ),
    )

async def require_admin(callback: CallbackQuery, app_context: ApplicationContext) -> bool:
    if (
        callback.from_user
        and callback.message
        and callback.message.chat.type == ChatType.PRIVATE
        and app_context.is_admin(callback.from_user.id)
    ):
        return True
    if callback.message and callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("استخدم لوحة الإدارة في المحادثة الخاصة.", show_alert=True)
        return False
    await callback.answer("هذا الأمر مخصص للإدارة فقط.", show_alert=True)
    return False


async def _is_required_channel_member(
    bot: Bot, app_context: ApplicationContext, user_id: int
) -> bool | None:
    if not app_context.settings.required_channel:
        return True
    try:
        membership = await bot.get_chat_member(app_context.settings.required_channel, user_id)
    except Exception:
        logger.warning("حاول لاحقاً %s.", user_id, exc_info=True)
        return None
    return membership.status in {"member", "administrator", "creator"} or (
        membership.status == "restricted" and getattr(membership, "is_member", False)
    )


def _is_active_channel_member(member: object) -> bool:
    status = getattr(member, "status", None)
    return status in {"member", "administrator", "creator"} or (
        status == "restricted" and bool(getattr(member, "is_member", False))
    )


async def _revoke_referral_for_membership_change(
    app_context: ApplicationContext,
    referred_user_id: int,
    reason: str,
) -> None:
    service, session = await service_session(app_context)
    try:
        revoke = getattr(service, "revoke_referral_for_referred_user", None)
        if revoke is None:
            logger.warning("حاول لاحقاً.")
            return
        await revoke(referred_user_id, reason)
    except Exception:
        logger.exception("حاول لاحقاً. %s . %s.", referred_user_id, reason)
    finally:
        await session.close()


def _matches_required_channel(event: ChatMemberUpdated, app_context: ApplicationContext) -> bool:
    required_channel = app_context.settings.required_channel
    if not required_channel:
        return False
    configured = str(required_channel).lstrip("@")
    return str(event.chat.id) == str(required_channel) or (
        bool(event.chat.username) and event.chat.username.lstrip("@") == configured
    )


@router.chat_member()
async def required_channel_member_update(
    event: ChatMemberUpdated,
    app_context: ApplicationContext,
) -> None:
    if not _matches_required_channel(event, app_context):
        return
    if _is_active_channel_member(event.new_chat_member):
        return
    if not _is_active_channel_member(event.old_chat_member):
        return
    await _revoke_referral_for_membership_change(
        app_context,
        event.new_chat_member.user.id,
        "channel_left",
    )


@router.my_chat_member()
async def bot_private_membership_update(
    event: ChatMemberUpdated,
    app_context: ApplicationContext,
) -> None:
    if event.chat.type != ChatType.PRIVATE:
        return
    if getattr(event.new_chat_member, "status", None) != "kicked":
        return
    if getattr(event.old_chat_member, "status", None) == "kicked":
        return
    await _revoke_referral_for_membership_change(app_context, event.chat.id, "bot_blocked")


@router.message(CommandStart())
async def start(
    message: Message, state: FSMContext, app_context: ApplicationContext, bot: Bot
) -> None:
    if not is_private(message):
        await message.answer("استخدم البوت في المحادثة الخاصة حفاظاً على بياناتك.")
        return
    referral_code = None
    if message.text:
        parts = message.text.split(maxsplit=1)
        referral_code = parts[1].strip() if len(parts) == 2 else None
    pending_referral_code = (await state.get_data()).get("pending_referral_code")
    referral_code = referral_code or (
        pending_referral_code if isinstance(pending_referral_code, str) else None
    )
    is_admin = app_context.is_admin(message.from_user.id)
    membership = True if is_admin else await _is_required_channel_member(bot, app_context, message.from_user.id)
    if membership is not True:
        if membership is False:
            await _revoke_referral_for_membership_change(
                app_context,
                message.from_user.id,
                "channel_left",
            )
        await state.clear()
        if referral_code:
            await state.update_data(pending_referral_code=referral_code)
        await message.answer(
            "<b>✅الـوسـاطـة الـرقـمـيـة الـسـوريـة✅\n\n\n● لـلإسـتـمـرار...\nانـضـم إلـى الـقـنـاة الـرسـمـيـة\nثـم اضـغـط «تـحـقـق مـن الاشـتـراك»</b>\n",
            reply_markup=subscription_menu(app_context.settings.required_channel_url),
        )
        return
    service, session = await service_session(app_context)
    try:
        await service.register_activation(message.from_user.id, None if is_admin else referral_code)
        monitoring = await service.monitoring_enabled() if is_admin else None
    finally:
        await session.close()
    if is_admin:
        await state.clear()
        await message.answer(
            "<b>لوحة التحكم</b>",
            reply_markup=admin_home_menu(monitoring=monitoring),
        )
        return
    await show_home(message, app_context, state)


@router.message(Command("admin"))
async def admin_command(message: Message, app_context: ApplicationContext) -> None:
    if not is_private(message):
        return
    if not message.from_user or not app_context.is_admin(message.from_user.id):
        return
    service, session = await service_session(app_context)
    try:
        monitoring = await service.monitoring_enabled()
        await message.answer("<b>لوحة التحكم</b>", reply_markup=admin_home_menu(monitoring=monitoring))
    finally:
        await session.close()

@router.message(Command("contact"))
async def contact_command(message: Message, app_context: ApplicationContext) -> None:
    if not is_private(message):
        return

    if app_context.settings.contact_url:
        await message.answer(
            f"للتواصل معنا: {html.escape(app_context.settings.contact_url)}"
        )
    else:
        await message.answer("حاول لاحقاً.")

@router.callback_query(F.data == "menu:rates")
async def refresh_rates_callback(
    callback: CallbackQuery,
    app_context: ApplicationContext,
) -> None:
    if not callback.message or not is_private(callback):
        await callback.answer("استخدم البوت في المحادثة الخاصة.", show_alert=True)
        return

    # يوقف مؤشر انتظار الزر قبل طلب السعر من الموقع.
    await callback.answer("جارٍ تحديث الأسعار...")

    is_admin = bool(
        callback.from_user
        and app_context.is_admin(callback.from_user.id)
    )

    try:
        prices_text = await sp_today_home_text(force_refresh=True)
    except DomainError:
        prices_text = (
            "<b>تعذر جلب الأسعار حالياً.</b>\n"
            "حاول تحديثها بعد قليل."
        )
    home_text = build_home_text(prices_text)

    try:
        await callback.message.edit_text(
            text=home_text,
            reply_markup=main_menu(
                is_admin,
                app_context.settings.contact_url,
                include_rates_refresh=True,

            ),
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise

@router.callback_query(F.data == "menu:home")
async def home_callback(callback: CallbackQuery, state: FSMContext, app_context: ApplicationContext) -> None:
    if not callback.message or not is_private(callback):
        await callback.answer("استخدم البوت في الخاص.", show_alert=True)
        return
    if await block_navigation_during_active_request(callback, state):
        return
    if state:
        await state.clear()

    is_admin = bool(callback.from_user and app_context.is_admin(callback.from_user.id))

    if is_admin:
        await admin_home(callback, state, app_context)
        return

    await callback.answer()

    try:
        await callback.message.delete()
    except TelegramAPIError:
        logger.warning("حاول لاحقاً.")
    try:
        prices_text = await sp_today_home_text()
    except DomainError:
        prices_text = (
            "<b>تعذر جلب الأسعار حالياً.</b>\n"
            "حاول تحديثها بعد قليل."
        )
    home_text = build_home_text(prices_text)

    try:
        await callback.message.answer(
            text=home_text,
            reply_markup=main_menu(
                is_admin,
                app_context.settings.contact_url,
                include_rates_refresh=True,
            ),
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise

@router.callback_query(F.data == "subscription:check")
async def subscription_check(
    callback: CallbackQuery, state: FSMContext, app_context: ApplicationContext
) -> None:
    """يفتح القائمة بعد مرور التحديث عبر فحص الاشتراك في الوسيط."""
    if not callback.message or not is_private(callback):
        await callback.answer("استخدم البوت في الخاص.", show_alert=True)
        return
    pending_referral_code = (await state.get_data()).get("pending_referral_code")
    service, session = await service_session(app_context)
    try:
        await service.register_activation(
            callback.from_user.id,
            pending_referral_code if isinstance(pending_referral_code, str) else None,
        )
    finally:
        await session.close()
    await callback.answer("✅تــم الــتــحــقــق مــن الاشــتــراك✅")
    await state.clear()
    try:
        await callback.message.delete()
    except TelegramAPIError:
        logger.info("حاول لاحقاً.")
    await callback.message.answer(
        "<b>✅تـم تـفـعـيـل حـسـابـك بـنـجـاح✅\n\nيـمـكـنـك الآن اسـتـخـدام خـدمـات الـبـوت.\n\nاضـعـط عـلـى الـزر أدنـاه لـلـمـتـابـعـة.</b>",
        reply_markup=start_only_menu(),
    )

@router.callback_query(F.data == "menu:convert")
async def convert_menu(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.message or not is_private(callback):
        await callback.answer("استخدم البوت في الخاص.", show_alert=True)
        return
    if await prepare_calculator_navigation(callback, state):
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except TelegramAPIError:
        logger.warning("حاول لاحقاً.")

    try:
        prices_text = await sp_today_home_text()
    except DomainError:
        prices_text = (
            "<b>تعذر جلب الأسعار حالياً.</b>\n"
            "حاول تحديثها بعد قليل."
        )
    await callback.message.answer(
        f"<b>{html.escape(app_context.settings.service_margin_notice)}</b>\n\n"
        f"{prices_text}\n\n"
        "<b>🟢 اخـتـر الـعـمـلـة الـتـي تـحـتـاجـهـا</b> 📥 📥",
        reply_markup=receive_currency_menu(),
    )

@router.callback_query(F.data == "menu:calculator")
async def calculator_menu(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.message or not is_private(callback):
        await callback.answer("استخدم البوت في الخاص.", show_alert=True)
        return

    if await prepare_calculator_navigation(callback, state):
        return
    await callback.answer()
    await state.clear()
    await state.set_state(UserFlow.awaiting_quick_calc_mode)
    await callback.message.edit_text(
        "<b>🧮 احـسـب هـنـا ⚡️</b>\n\n\n"
        "<b>● اختر ما تريد احتسابه، وسيقوم النظام بـ: 📝</b>\n\n"
        "<b>● تقدير القيمة بناءً على سعر الصرف الحالي 📈</b>\n\n"
        "<b>● إصدار تقرير كامل وشامل لكافة التفاصيل 📋</b>\n\n\n"
        "(ملاحظة: هذه الحاسبة مخصصة للمعرفة والاطلاع فقط)",
        reply_markup=quick_calculator_mode_menu(),
    )
    await state.update_data(
        prompt_chat_id=callback.message.chat.id,
        prompt_message_id=callback.message.message_id,
    )


@router.message(UserFlow.awaiting_quick_calc_mode, F.text)
async def require_quick_calculator_mode(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if message.chat.id != data.get("prompt_chat_id"):
        return
    try:
        await message.delete()
    except TelegramAPIError:
        logger.info("تعذر حذف إدخال سبق اختيار طريقة الحاسبة.")
    prompt_message_id = data.get("prompt_message_id")
    if isinstance(prompt_message_id, int):
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=prompt_message_id,
            text=(
                "<b>🧮 احـسـب هـنـا ⚡️</b>\n\n\n"
                "اإخـتـر طـريـقـة الـحـسـاب أولاً."
            ),
            reply_markup=quick_calculator_mode_menu(),
        )


@router.message(UserFlow.awaiting_quick_calc_routes, F.text)
async def require_quick_calculator_route(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if message.chat.id != data.get("prompt_chat_id"):
        return
    try:
        await message.delete()
    except TelegramAPIError:
        logger.info("تعذر حذف إدخال سبق اختيار مسار الحاسبة.")
    prompt_message_id = data.get("prompt_message_id")
    if isinstance(prompt_message_id, int):
        route_codes = data.get("quick_calc_routes")
        routes = []
        if isinstance(route_codes, list):
            for code in route_codes:
                if not isinstance(code, str):
                    continue
                try:
                    routes.append(RouteCode(code))
                except ValueError:
                    continue
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=prompt_message_id,
            text="<b>🧮 احـسـب هـنـا ⚡️</b>\n\n\nإخـتـر طـلـبـك",
            reply_markup=quick_calculator_routes_menu(routes),
        )


@router.message(UserFlow.quick_calc_processing, F.text)
async def discard_quick_calculator_input_while_loading(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError:
        logger.info("تعذر حذف إدخال وصل أثناء حساب السعر.")


@router.callback_query(F.data.startswith("convert:to:"))
async def choose_receive_currency(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.message or not is_private(callback):
        await callback.answer("استخدم البوت في المحادثة الخاصة.", show_alert=True)
        return
    if await prepare_calculator_navigation(callback, state):
        return

    try:
        target_currency = Currency((callback.data or "").removeprefix("convert:to:"))
    except ValueError:
        await callback.answer("اختيار العملة غير صالح.", show_alert=True)
        return

    service, session = await service_session(app_context)
    try:
        routes = [
            route for route in await service.enabled_routes()
            if route.to_currency == target_currency
        ]
    finally:
        await session.close()

    currency_names = {
        Currency.SYP: "\n\nأحـتـاج ● شـام كـاش ل.س ● ، سـأُرسـل",
        Currency.USD: "\n\nأحـتـاج ● شـام كـاش دولار ● ، سـأُرسـل",
        Currency.USDT: "\n\nأحـتـاج ● عـمـلات رقــمـيـة ● ، سـأُرسـل",
    }

    await callback.answer()

    if not routes:
        try:
            await callback.message.delete()
        except TelegramAPIError:
            logger.warning("حاول لاحقاً.")
        await callback.message.answer(
            "حاول لاحقاً.",
            reply_markup=receive_currency_menu(),
        )
        return

    try:
        await callback.message.delete()
    except TelegramAPIError:
        logger.warning("حاول لاحقاً.")
    try:
        prices_text = await sp_today_home_text()
    except DomainError:
        prices_text = (
            "<b>تعذر جلب الأسعار حالياً.</b>\n"
            "حاول تحديثها بعد قليل."
        )
    await callback.message.answer(
        f"<b>{html.escape(app_context.settings.service_margin_notice)}</b>\n\n"
        f"{prices_text}\n\n"
        f"<b>🔴 اخـتـر الـعـمـلـة الـتـي سـتـرسـلـهـا</b> 📤 📤{currency_names[target_currency]}:",
        reply_markup=send_currency_menu(routes),
    )


async def _show_quick_calculator_routes(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
    mode: str,
) -> None:
    service, session = await service_session(app_context)
    try:
        routes = await service.enabled_routes()
    finally:
        await session.close()
    data = await state.get_data()
    await state.set_state(UserFlow.awaiting_quick_calc_routes)
    await state.update_data(
        quick_calc_mode=mode,
        quick_calc_routes=[route.code.value for route in routes],
        quick_calc_route=None,
    )
    mode_label = "مبلغ للإرسال" if mode == "send" else "صافي مبلغ للاستلام"
    await callback.message.edit_text(
        f"<b>🧮 احـسـب هـنـا ⚡️</b>\n\n\n"
        f"طـلـبـك: <b>{mode_label}</b>\n\n"
        "اختر طريقة التحويل المراد احتسابها.",
        reply_markup=quick_calculator_routes_menu(routes),
    )


@router.callback_query(F.data.startswith("quickcalc:mode:"))
async def choose_quick_calculator_mode(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.message or not is_private(callback):
        await callback.answer("استخدم البوت في الخاص.", show_alert=True)
        return
    data = await state.get_data()
    if (
        await state.get_state() != UserFlow.awaiting_quick_calc_mode.state
        or callback.message.message_id != data.get("prompt_message_id")
        or callback.message.chat.id != data.get("prompt_chat_id")
    ):
        await callback.answer("هذه شاشة حاسبة قديمة. ابدأ من جديد.", show_alert=True)
        return
    mode = (callback.data or "").removeprefix("quickcalc:mode:")
    if mode not in {"send", "receive"}:
        await callback.answer("طريقة الحساب غير صالحة.", show_alert=True)
        return
    await callback.answer()
    await _show_quick_calculator_routes(callback, state, app_context, mode)


@router.callback_query(F.data == "quickcalc:change-mode")
async def quick_calculator_change_mode(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if not callback.message or not is_private(callback):
        await callback.answer("استخدم البوت في الخاص.", show_alert=True)
        return
    data = await state.get_data()
    if (
        await state.get_state() not in {
            UserFlow.awaiting_quick_calc_routes.state,
            UserFlow.awaiting_quick_calc_amount.state,
        }
        or callback.message.message_id != data.get("prompt_message_id")
        or callback.message.chat.id != data.get("prompt_chat_id")
    ):
        await callback.answer("هذه شاشة حاسبة قديمة. ابدأ من جديد.", show_alert=True)
        return
    await state.set_state(UserFlow.awaiting_quick_calc_mode)
    await callback.answer()
    await callback.message.edit_text(
        "<b>🧮 احـسـب هـنـا ⚡️</b>\n\n\n"
        "<b>● اختر ما تريد احتسابه، وسيقوم النظام بـ: 📝</b>\n\n"
        "<b>● تقدير القيمة بناءً على سعر الصرف الحالي 📈</b>\n\n"
        "<b>● إصدار تقرير كامل وشامل لكافة التفاصيل 📋</b>\n\n\n"
        "(ملاحظة: هذه الحاسبة مخصصة للمعرفة والاطلاع فقط)",
        reply_markup=quick_calculator_mode_menu(),
    )


async def _quick_calculator_show_routes(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    data = await state.get_data()
    mode = str(data.get("quick_calc_mode", ""))
    if mode not in {"send", "receive"}:
        await callback.answer("اختر طريقة الحساب أولاً.", show_alert=True)
        return
    service, session = await service_session(app_context)
    try:
        routes = await service.enabled_routes()
    finally:
        await session.close()
    await state.set_state(UserFlow.awaiting_quick_calc_routes)
    await state.update_data(quick_calc_routes=[route.code.value for route in routes])
    await callback.message.edit_text(
        "<b>🧮 احـسـب هـنـا ⚡️</b>\n\n\nإخـتـر طـلـبـك",
        reply_markup=quick_calculator_routes_menu(routes),
    )


@router.callback_query(F.data == "quickcalc:change-route")
async def quick_calculator_change_route(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.message or not is_private(callback):
        await callback.answer("استخدم البوت في الخاص.", show_alert=True)
        return
    data = await state.get_data()
    if (
        await state.get_state() != UserFlow.awaiting_quick_calc_amount.state
        or callback.message.message_id != data.get("prompt_message_id")
        or callback.message.chat.id != data.get("prompt_chat_id")
    ):
        await callback.answer("هذه شاشة حاسبة قديمة.", show_alert=True)
        return
    await callback.answer()
    await _quick_calculator_show_routes(callback, state, app_context)


@router.callback_query(F.data.startswith("quickcalc:route:"))
async def choose_quick_calculator_route(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.message or not is_private(callback):
        await callback.answer("استخدم البوت في الخاص.", show_alert=True)
        return
    data = await state.get_data()
    if (
        await state.get_state() != UserFlow.awaiting_quick_calc_routes.state
        or callback.message.message_id != data.get("prompt_message_id")
        or callback.message.chat.id != data.get("prompt_chat_id")
    ):
        await callback.answer("هذه شاشة حاسبة قديمة. ابدأ من جديد.", show_alert=True)
        return
    try:
        route_code = RouteCode((callback.data or "").removeprefix("quickcalc:route:"))
    except ValueError:
        await callback.answer("المسار غير صالح.", show_alert=True)
        return
    allowed_routes = data.get("quick_calc_routes")
    if not isinstance(allowed_routes, list) or route_code.value not in allowed_routes:
        await callback.answer("هذا المسار غير متاح حالياً.", show_alert=True)
        return
    await state.set_state(UserFlow.awaiting_quick_calc_amount)
    await state.update_data(quick_calc_route=route_code.value)
    mode = str(data.get("quick_calc_mode"))
    instruction = (
        "أرسل المبلغ المتوفر لديك للإرسال."
        if mode == "send"
        else "أرسل صافي المبلغ الذي تريد استلامه."
    )
    await callback.answer()
    await callback.message.edit_text(
        f"<b>🧮 احـسـب هـنـا ⚡️</b>\n\n\n"
        f"الـطـلـب: <b>\n{html.escape(ROUTE_LABELS[route_code])}</b>\n\n"
        f"{instruction}\n\n(ملاحظة: هذه الحاسبة مخصصة للمعرفة والاطلاع فقط)",
        reply_markup=quick_calculator_result_menu(),
    )


@router.message(UserFlow.awaiting_quick_calc_amount, F.text)
async def receive_quick_calculator_amount(
    message: Message,
    state: FSMContext,
    app_context: ApplicationContext,
    bot: Bot,
) -> None:
    if not message.from_user:
        return
    lock = _QUICK_CALC_LOCKS.setdefault(message.from_user.id, asyncio.Lock())
    async with lock:
        data = await state.get_data()
        if (
            message.chat.id != data.get("prompt_chat_id")
            or await state.get_state() != UserFlow.awaiting_quick_calc_amount.state
        ):
            return
        try:
            amount = decimal_from_text(message.text or "")
            route_code = RouteCode(str(data.get("quick_calc_route")))
            mode = str(data.get("quick_calc_mode"))
            if mode not in {"send", "receive"}:
                raise DomainError("اختر طريقة الحساب أولاً.")
        except (ValueError, DomainError) as error:
            try:
                await message.delete()
            except TelegramAPIError:
                logger.info("تعذر حذف رقم الحاسبة غير الصالح.")
            prompt_message_id = data.get("prompt_message_id")
            if isinstance(prompt_message_id, int):
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=prompt_message_id,
                    text=(
                        f"<b>🧮 احـسـب هـنـا ⚡️</b>\n\n\n"
                        f"{html.escape(str(error))}\nأرسل مبلغاً صحيحاً بالأرقام فقط."
                    ),
                    reply_markup=quick_calculator_result_menu(),
                )
            return

        await state.set_state(UserFlow.quick_calc_processing)
        try:
            service, session = await service_session(app_context)
        except Exception:
            if await state.get_state() == UserFlow.quick_calc_processing.state:
                await state.set_state(UserFlow.awaiting_quick_calc_amount)
            raise
        try:
            route = await service.get_route(route_code)
            rates = await service.current_rates()
            if not route.is_enabled:
                raise DomainError("هذا المسار متوقف مؤقتاً.")
            rate = await live_rate_for_route(route_code)
            fee = app_context.settings.global_fee_percentage
            if mode == "send":
                received, _ = calculate_route_quote(
                    amount=amount,
                    route_code=route_code,
                    rate=rate,
                    fee_percentage=fee,
                    minimum_usd=rates.minimum_usd,
                )
                from_currency, to_currency = route.from_currency, route.to_currency
                rounded_amount = amount.quantize(quantum_for(from_currency), rounding=ROUND_DOWN)
                if route_code in {RouteCode.SYP_TO_USD, RouteCode.SYP_TO_USDT}:
                    gross_raw = rounded_amount / rate
                elif route_code in {RouteCode.USD_TO_SYP, RouteCode.USDT_TO_SYP}:
                    gross_raw = rounded_amount * rate
                else:
                    gross_raw = rounded_amount
                gross = gross_raw.quantize(quantum_for(to_currency), rounding=ROUND_DOWN)
                payment = rounded_amount
                net = received
                entered_text = ""
            else:
                payment, gross, fee_amount, net = calculate_target_breakdown(
                    target_amount=amount,
                    route_code=route_code,
                    rate=rate,
                    fee_percentage=fee,
                    minimum_usd=rates.minimum_usd,
                    usdt_per_usd=Decimal("1"),
                )
                from_currency, to_currency = route.from_currency, route.to_currency
                entered_text = f"صافي الاستلام المطلوب: <b>{format_amount(amount, to_currency)}</b>"
            if mode == "send":
                fee_amount = gross - net
        except DomainError as error:
            if await state.get_state() == UserFlow.quick_calc_processing.state:
                await state.set_state(UserFlow.awaiting_quick_calc_amount)
            try:
                await message.delete()
            except TelegramAPIError:
                logger.info("تعذر حذف مبلغ الحاسبة.")
            if await state.get_state() == UserFlow.awaiting_quick_calc_amount.state:
                await bot.edit_message_text(
                    chat_id=data["prompt_chat_id"],
                    message_id=data["prompt_message_id"],
                    text=(
                        f"الـطـلـب: <b>{html.escape(ROUTE_LABELS[route_code])}</b>\n\n"
                        f"{html.escape(str(error))}\nأرسل مبلغاً آخر للحساب."
                    ),
                    reply_markup=quick_calculator_result_menu(),
                )
            return
        except Exception:
            if await state.get_state() == UserFlow.quick_calc_processing.state:
                await state.set_state(UserFlow.awaiting_quick_calc_amount)
            raise
        finally:
            await session.close()

        if await state.get_state() != UserFlow.quick_calc_processing.state:
            return
        try:
            await message.delete()
        except TelegramAPIError:
            logger.info("تعذر حذف رسالة المبلغ بعد الحساب.")
        result_text = (
            "<b>🧮 تـقـريـر الاحـتـسـاب:</b>\n\n\n"
            f"الـطـلـب: <b>\n{html.escape(ROUTE_LABELS[route_code])}</b>\n\n"
            f"{entered_text}\n\n"
            f"مبلغ الدفع المطلوب: <b>{format_amount(payment, from_currency)}</b>\n\n"
            f"قيمة التحويل: <b>{format_amount(gross, to_currency)}</b>\n\n"
            f"عمولة الخدمة ({format_rate(fee)}%): <b>{format_amount(fee_amount, to_currency)}</b>\n\n"
            f"صافي الاستلام: <b>{format_amount(net, to_currency)}</b>\n\n"
            f"سعر الصرف: <b>{comparison_rate_text(route_code, rate)}</b>\n\n\n"
            "هذا التقرير يتناسب مع سعر الصرف الحالي.\n"
            "أرسل مبلغاً آخر للحساب على هذه الرسالة."
            
        )
        await state.set_state(UserFlow.awaiting_quick_calc_amount)
        await state.update_data(quick_calc_amount=str(amount))
        await bot.edit_message_text(
            chat_id=data["prompt_chat_id"],
            message_id=data["prompt_message_id"],
            text=result_text,
            reply_markup=quick_calculator_result_menu(),
        )


def comparison_rate_text(route_code: RouteCode, rate: Decimal) -> str:
    if route_code is RouteCode.USDT_TO_SYP:
        return f"1 USDT ≈ {format_rate(rate)} ل.س"
    if route_code in {RouteCode.SYP_TO_USD, RouteCode.USD_TO_SYP}:
        return f"1 دولار ≈ {format_rate(rate)} ل.س"
    if route_code is RouteCode.SYP_TO_USDT:
        return f"1 دولار ≈ {format_rate(rate)} ل.س (وUSDT≈الدولار)"
    if route_code is RouteCode.USDT_TO_USD:
        return "لدولار≈USDT"
    return "الدولار≈USDT"


@router.callback_query(F.data == "menu:compare")
async def compare_menu(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.message or not is_private(callback):
        await callback.answer("استخدم البوت في الخاص.", show_alert=True)
        return
    if await prepare_calculator_navigation(callback, state):
        return

    await state.clear()
    await state.set_state(UserFlow.awaiting_compare_currency)
    await callback.answer()
    await callback.message.edit_text(
        "<b>💡 قـارن وعـايـن 🔍</b>\n\n\n"
		"<b>● اختر العملة المتوفرة لديك وترغب في مقارنتها، أو حساب قيمتها، أو إيجاد الصيغة المناسبة لك.</b>\n\n"
		"<b>● سيقوم النظام بتقدير القيم تلقائياً بناءً على أسعار الصرف الحية 📈</b>\n\n"
		"<b>● وإصدار تقرير يغطي كافة تفاصيل عملياتك المالية الممكنة 📋 </b>\n\n",
        reply_markup=comparison_source_menu(),
    )
    await state.update_data(
        prompt_chat_id=callback.message.chat.id,
        prompt_message_id=callback.message.message_id,
    )


@router.message(UserFlow.awaiting_compare_currency, F.text)
async def require_compare_currency(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if message.chat.id != data.get("prompt_chat_id"):
        return
    try:
        await message.delete()
    except TelegramAPIError:
        logger.info("تعذر حذف إدخال سبق اختيار فئة المقارنة.")
    prompt_message_id = data.get("prompt_message_id")
    if isinstance(prompt_message_id, int):
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=prompt_message_id,
            text=(
                "<b>💡 قـارن وعـايـن 🔍</b>\n\n"
                "اختر العملة المتوفرة لديك أولاً."
            ),
            reply_markup=comparison_source_menu(),
        )


@router.message(UserFlow.awaiting_compare_processing, F.text)
async def discard_compare_input_while_loading(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError:
        logger.info("تعذر حذف إدخال وصل أثناء تحديث المقارنة.")


@router.callback_query(F.data.startswith("compare:source:"))
async def choose_compare_source(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.from_user:
        await callback.answer("تعذر تحديد المستخدم.", show_alert=True)
        return
    lock = _COMPARISON_LOCKS.setdefault(callback.from_user.id, asyncio.Lock())
    async with lock:
        await _choose_compare_source_locked(callback, state, app_context)


async def _choose_compare_source_locked(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.message or not is_private(callback):
        await callback.answer("استخدم البوت في الخاص.", show_alert=True)
        return
    data = await state.get_data()
    current_state = await state.get_state()
    if (
        current_state not in {
            UserFlow.awaiting_compare_currency.state,
            UserFlow.awaiting_compare_amount.state,
        }
        or callback.message.message_id != data.get("prompt_message_id")
        or callback.message.chat.id != data.get("prompt_chat_id")
    ):
        await callback.answer("هذه شاشة مقارنة قديمة. ابدأ المقارنة من جديد.", show_alert=True)
        return

    category = (callback.data or "").removeprefix("compare:source:")
    if category not in {"SYP", "USD"}:
        await callback.answer("اختيار العملة غير صالح.", show_alert=True)
        return

    source_currencies = (
        {Currency.SYP} if category == "SYP" else {Currency.USD, Currency.USDT}
    )
    await state.set_state(UserFlow.awaiting_compare_processing)
    await callback.answer("جارٍ تحميل الأسعار...")
    try:
        service, session = await service_session(app_context)
    except Exception:
        if await state.get_state() == UserFlow.awaiting_compare_processing.state:
            await state.set_state(
                UserFlow.awaiting_compare_currency
                if current_state == UserFlow.awaiting_compare_currency.state
                else UserFlow.awaiting_compare_amount
            )
        raise
    try:
        routes = [
            route for route in await service.enabled_routes()
            if route.from_currency in source_currencies
        ]
        rate_lines: list[str] = []
        for route in routes:
            try:
                rate = await live_rate_for_route(route.code)
                rate_lines.append(
                    f"• {html.escape(ROUTE_LABELS[route.code])}\n"
                    f"  {comparison_rate_text(route.code, rate)}"
                )
            except DomainError:
                rate_lines.append(
                    f"• {html.escape(ROUTE_LABELS[route.code])}\n  تعذر جلب السعر الآن."
                )
    except Exception:
        if await state.get_state() == UserFlow.awaiting_compare_processing.state:
            await state.set_state(
                UserFlow.awaiting_compare_currency
                if current_state == UserFlow.awaiting_compare_currency.state
                else UserFlow.awaiting_compare_amount
            )
        raise
    finally:
        await session.close()

    if await state.get_state() != UserFlow.awaiting_compare_processing.state:
        return

    if category == "SYP":
        selected_text = "<b>اخترت: ليرة سورية (شام كاش)</b>"
        amount_hint = "أرسل المبلغ المتوفر لديك للإرسال."
    else:
        selected_text = "<b>اخترت: دولار (شام كاش و USDT)</b>"
        amount_hint = (
            "أرسل المبلغ بالأرقام؛ سأعرض نتيجتين منفصلتين إذا كان المبلغ دولاراً "
            "أو USDT."
        )
    rates_text = "\n\n<b>الأسعار الحيّة:</b>\n" + (
        "\n\n".join(rate_lines) if rate_lines else "لا توجد مسارات مفعّلة لهذه الفئة حالياً."
    )
    await state.set_state(UserFlow.awaiting_compare_amount)
    await state.update_data(
        compare_category=category,
        compare_amount=None,
        compare_routes=[route.code.value for route in routes],
    )
    await callback.message.edit_text(
        f"<b>💡 قـارن وعـايـن 🔍</b>\n\n\n{selected_text}\n\n"
        f"{amount_hint}{rates_text}\n\n"
        "<b>أرسل المبلغ الذي تريد معاينته.</b>",
        reply_markup=comparison_source_menu(),
    )


@router.message(UserFlow.awaiting_compare_amount, F.text)
async def receive_compare_amount(
    message: Message,
    state: FSMContext,
    app_context: ApplicationContext,
    bot: Bot,
) -> None:
    if not message.from_user:
        return
    lock = _COMPARISON_LOCKS.setdefault(message.from_user.id, asyncio.Lock())
    async with lock:
        await _receive_compare_amount_locked(message, state, app_context, bot)


async def _receive_compare_amount_locked(
    message: Message,
    state: FSMContext,
    app_context: ApplicationContext,
    bot: Bot,
) -> None:
    if not message.from_user or not is_private(message):
        return
    data = await state.get_data()
    if (
        message.chat.id != data.get("prompt_chat_id")
        or await state.get_state() != UserFlow.awaiting_compare_amount.state
    ):
        return
    try:
        category = str(data.get("compare_category"))
        if category not in {"SYP", "USD"}:
            raise ValueError("اختيار العملة غير صالح.")
        amount = decimal_from_text(message.text or "")
    except (ValueError, DomainError) as error:
        try:
            await message.delete()
        except TelegramAPIError:
            logger.info("تعذر حذف رسالة مبلغ المقارنة غير الصالح.")
        prompt_message_id = data.get("prompt_message_id")
        if isinstance(prompt_message_id, int):
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=prompt_message_id,
                text=(
                    f"<b>🔎 قارن لي</b>\n\n{html.escape(str(error))}\n\n"
                    "أرسل مبلغاً صحيحاً، وستبقى النتائج على هذه الرسالة."
                ),
                reply_markup=comparison_source_menu(),
            )
        return

    await state.set_state(UserFlow.awaiting_compare_processing)

    service, session = await service_session(app_context)
    results: list[tuple[ExchangeRoute, Decimal, Decimal]] = []
    errors: list[tuple[ExchangeRoute | None, str]] = []
    source_currencies = (
        {Currency.SYP} if category == "SYP" else {Currency.USD, Currency.USDT}
    )
    try:
        routes = [
            route for route in await service.enabled_routes()
            if route.from_currency in source_currencies
        ]
        rates = await service.current_rates()
        if not routes:
            errors.append((None, "لا توجد مسارات مفعّلة لهذه الفئة حالياً."))
        for route in routes:
            try:
                rate = await live_rate_for_route(route.code)
                received, _ = calculate_route_quote(
                    amount=amount,
                    route_code=route.code,
                    rate=rate,
                    fee_percentage=app_context.settings.global_fee_percentage,
                    minimum_usd=rates.minimum_usd,
                )
                results.append((route, received, rate))
            except DomainError as error:
                errors.append((route, str(error)))
    except (DomainError, RuntimeError) as error:
        errors.append((None, str(error)))
    except Exception:
        if await state.get_state() == UserFlow.awaiting_compare_processing.state:
            await state.set_state(UserFlow.awaiting_compare_amount)
        raise
    finally:
        await session.close()

    if await state.get_state() != UserFlow.awaiting_compare_processing.state:
        return

    try:
        await message.delete()
    except TelegramAPIError:
        logger.info("تعذر حذف رسالة مبلغ المقارنة.")

    if not results:
        await state.set_state(UserFlow.awaiting_compare_amount)
        prompt_message_id = data.get("prompt_message_id")
        if isinstance(prompt_message_id, int):
            error_text = "\n".join(
                html.escape(error) for _, error in errors
            ) or "تعذر احتساب النتائج بهذا المبلغ."
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=prompt_message_id,
                text=(
                    f"<b>🔎 قارن لي</b>\n\n{error_text}\n\n"
                    "أرسل مبلغاً آخر، وستتحدث هذه الرسالة نفسها."
                ),
                reply_markup=comparison_source_menu(),
            )
        return

    if category == "SYP":
        entered_amount_text = format_amount(amount, Currency.SYP)
        amount_note = ""
    else:
        entered_amount_text = format_amount(amount, Currency.USD)
        amount_note = "سيُحسب الرقم مرةً كدولار شام كاش ومرةً كـ USDT.\n"
    result_text = (
        "<b>🔎 نتيجة المقارنة</b>\n\n"
        f"المبلغ المدخل: <b>{entered_amount_text}</b>\n"
        f"{amount_note}"
        f"عمولة الخدمة: <b>{format_rate(app_context.settings.global_fee_percentage)}%</b>\n\n"
    )
    if category == "SYP":
        result_text += "<b>● إذا أرسلت المبلغ عبر شام كاش ليرة:</b>\n\n"
        grouped_results = [(Currency.SYP, results)]
    else:
        grouped_results = [
            (currency, [item for item in results if item[0].from_currency is currency])
            for currency in (Currency.USD, Currency.USDT)
        ]
    source_names = {
        Currency.SYP: "شام كاش ل.س",
        Currency.USD: "شام كاش دولار",
        Currency.USDT: "USDT",
    }
    for source_currency, source_results in grouped_results:
        if category == "USD":
            result_text += (
                f"<b>إذا كان المبلغ {source_names[source_currency]}:</b>\n"
            )
        for route, received, rate in source_results:
            result_text += (
                f"• {html.escape(ROUTE_LABELS[route.code])}\n"
                f"  سعر الصرف: {comparison_rate_text(route.code, rate)}\n"
                f"  صافي الاستلام: <b>{format_amount(received, route.to_currency)}</b>\n"
            )
        if source_results:
            result_text += "\n"
    if errors:
        result_text += "<i>تعذر احتساب بعض الاسعار:</i>\n"
        for route, error in errors:
            route_label = (
                f"{html.escape(ROUTE_LABELS[route.code])}: " if route else ""
            )
            result_text += f"• {route_label}{html.escape(error)}\n"
        result_text += "\n"
    result_text += "أرسل مبلغاً آخر للحساب.\n\nأو اختر فئة عملة مختلفة من الأزرار."

    await state.set_state(UserFlow.awaiting_compare_amount)
    await state.update_data(
        compare_amount=str(amount),
        compare_category=category,
        compare_routes=[route.code.value for route, _, _ in results],
    )
    await bot.edit_message_text(
        chat_id=data["prompt_chat_id"],
        message_id=data["prompt_message_id"],
        text=result_text,
        reply_markup=comparison_source_menu(),
    )


@router.callback_query(F.data.startswith("compare:choose:"))
async def choose_compare_result(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    await callback.answer(
        "المقارنة للعرض والحساب فقط، ولا تنتقل منها إلى طلب. استخدم زر تحويل العملات لبدء طلب جديد.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("route:"))
async def choose_route(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        await callback.answer("استخدم البوت في المحادثة الخاصة.", show_alert=True)
        return
    if await prepare_calculator_navigation(callback, state):
        return

    try:
        route_code = RouteCode((callback.data or "").removeprefix("route:"))
    except ValueError:
        await callback.answer("مسار غير صالح.", show_alert=True)
        return

    await callback.answer()
    await state.clear()
    
    await state.update_data(
        route_code=route_code.value,
        quote_mode=None,
    )

    await state.set_state(UserFlow.awaiting_quote_mode)

    try:
        await callback.message.delete()
    except TelegramAPIError:
        logger.warning("حاول لاحقاً.")
    try:
        prices_text = await sp_today_home_text()
    except DomainError:
        prices_text = (
            "<b>تعذر جلب الأسعار حالياً.</b>\n"
            "حاول تحديثها بعد قليل."
        )
    prompt_message = await callback.message.answer(
        f"{prices_text}\n\n"
        f"📝<b>الـطـلـب:</b>\n<b>{html.escape(ROUTE_LABELS[route_code])}</b>\n\n"
        "<b>🧮 اختر طريقة الحساب أولاً، ثم أرسل المبلغ. 🧮</b>",
        reply_markup=target_calculator_menu(mode="receive"),
    )
    await state.update_data(
        prompt_chat_id=prompt_message.chat.id,
        prompt_message_id=prompt_message.message_id,
    )

@router.message(UserFlow.awaiting_quote_mode, F.text)
async def require_quote_mode_before_amount(
    message: Message,
    state: FSMContext,
) -> None:
    if not message.from_user:
        return
    data = await state.get_data()
    try:
        route_code = RouteCode(str(data.get("route_code")))
    except ValueError:
        await state.clear()
        await message.answer("انتهت جلسة الحساب. ابدأ طلباً جديداً من /start.")
        return
    prompt_chat_id = data.get("prompt_chat_id")
    prompt_message_id = data.get("prompt_message_id")
    if not isinstance(prompt_chat_id, int) or not isinstance(prompt_message_id, int):
        await state.clear()
        await message.answer("انتهت جلسة الحساب. ابدأ طلباً جديداً من /start.")
        return
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    await message.bot.edit_message_text(
        chat_id=prompt_chat_id,
        message_id=prompt_message_id,
        text=(
            f"{request_route_line(route_code)}"
            "اختر طريقة الحساب من الأزرار أولاً، ثم أرسل المبلغ."
        ),
        reply_markup=target_calculator_menu(),
    )

@router.message(UserFlow.awaiting_target_amount, F.text)
async def receive_target_amount(
    message: Message,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not message.from_user:
        return

    data = await state.get_data()

    try:
        route_code = RouteCode(str(data.get("route_code")))
    except ValueError:
        await state.clear()
        try:
            await message.delete()
        except TelegramAPIError as error:
            logger.warning(
                "[E045] تعذّر حذف رسالة المستخدم بعد تسجيل مرجع الدفع: %s",
                error,
            )
        await message.answer("حاول لاحقاً. /start.")
        return

    if not data.get("editing_target") and data.get("quote_mode") not in {"receive", "send"}:
        prompt_chat_id = data.get("prompt_chat_id")
        prompt_message_id = data.get("prompt_message_id")
        if not isinstance(prompt_chat_id, int) or not isinstance(prompt_message_id, int):
            await state.clear()
            await message.answer("انتهت جلسة الحساب. ابدأ طلباً جديداً من /start.")
            return
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        await state.update_data(
            quote_mode=None,
            target_amount_input=None,
            target_quote_ready=False,
        )
        await state.set_state(UserFlow.awaiting_quote_mode)
        await message.bot.edit_message_text(
            chat_id=prompt_chat_id,
            message_id=prompt_message_id,
            text=(
                f"{request_route_line(route_code)}"
                "اختر طريقة الحساب أولاً، ثم أرسل المبلغ."
            ),
            reply_markup=target_calculator_menu(),
        )
        return

    if not data.get("editing_target"):
        raw_amount = (message.text or "").strip()
        prompt_chat_id = data.get("prompt_chat_id")
        prompt_message_id = data.get("prompt_message_id")

        if not isinstance(prompt_chat_id, int) or not isinstance(prompt_message_id, int):
            await state.clear()
            await message.answer("انتهت جلسة الحساب. ابدأ طلباً جديداً من /start.")
            return

        try:
            decimal_from_text(raw_amount)
        except DomainError as error:
            try:
                await message.delete()
            except TelegramAPIError:
                pass

            await message.bot.edit_message_text(
                chat_id=prompt_chat_id,
                message_id=prompt_message_id,
                text=(
                    f"📝<b>الـطـلـب:</b>\n<b>{html.escape(ROUTE_LABELS[route_code])}</b>\n\n"
                    f"{html.escape(str(error))}\n\n"
                    "أرسل المبلغ بالأرقام فقط بعد اختيار طريقة الحساب."
                ),
                reply_markup=target_calculator_menu(mode=str(data.get("quote_mode", "receive"))),
            )
            return

        await state.update_data(
            target_amount_input=raw_amount,
            target_quote_ready=False,
        )

        try:
            await message.delete()
        except TelegramAPIError:
            logger.info("تعذر حذف رسالة المبلغ التي أدخلها المستخدم.")

        await _show_target_quote(
            bot=message.bot,
            state=state,
            app_context=app_context,
            raw_amount=raw_amount,
            chat_id=message.chat.id,
            message_id=prompt_message_id,
        )
        return

    service, session = await service_session(app_context)
    try:
        if data.get("editing_target"):
            edit_mode = str(data.get("quote_mode", "receive"))
            if edit_mode == "send":
                request = await service.set_amount(
                    str(data.get("request_id")),
                    message.from_user.id,
                    message.text or "",
                )
            elif edit_mode == "receive":
                request = await service.set_target_amount(
                    str(data.get("request_id")),
                    message.from_user.id,
                    message.text or "",
                )
            else:
                raise DomainError("طريقة الحساب غير صالحة. ابدأ التعديل من جديد.")
            await state.update_data(editing_target=False)
        else:
            request = await service.create_target_draft(
                route_code=route_code,
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
                raw_target_amount=message.text or "",
            )
    except DomainError as error:
        try:
            await message.delete()
        except TelegramAPIError:
            logger.warning("حاول لاحقاً.")

        edit_mode = str(data.get("quote_mode", "receive"))
        amount_instruction = (
            "أعد إرسال المبلغ المتوفر لديك للإرسال، بالأرقام فقط."
            if edit_mode == "send"
            else "أعد إدخال صافي المبلغ الذي تريد استلامه، بالأرقام فقط."
        )
        prompt_chat_id = data.get("prompt_chat_id")
        prompt_message_id = data.get("prompt_message_id")
        if isinstance(prompt_chat_id, int) and isinstance(prompt_message_id, int):
            try:
                await message.bot.edit_message_text(
                    chat_id=prompt_chat_id,
                    message_id=prompt_message_id,
                    text=(
                        f"{request_route_line(route_code)}"
                        f"{html.escape(str(error))}\n\n{amount_instruction}"
                    ),
                    reply_markup=target_calculator_menu(
                        mode=edit_mode,
                        show_modes=False,
                    ),
                )
            except TelegramAPIError:
                logger.warning("تعذر تحديث رسالة تعديل المبلغ.")
        return
    finally:
        await session.close()

    try:
        await message.delete()
    except TelegramAPIError:
        logger.warning("حاول لاحقاً.")

    prompt_chat_id = data.get("prompt_chat_id")
    prompt_message_id = data.get("prompt_message_id")
    if isinstance(prompt_chat_id, int) and isinstance(prompt_message_id, int):
        try:
            await message.bot.delete_message(
                chat_id=prompt_chat_id,
                message_id=prompt_message_id,
            )
        except TelegramAPIError:
            logger.warning("حاول لاحقاً.")
    try:
        prices_text = await sp_today_home_text()
    except DomainError:
        prices_text = (
            "<b>تعذر جلب الأسعار حالياً.</b>\n"
            "حاول تحديثها بعد قليل."
        )
    await state.set_state(UserFlow.awaiting_payout_address)
    prompt_message = await message.answer(
        f"{prices_text}\n\n"
        f"{request_route_line(request.route_code)}"
        f"🔴<b>سـتـرسـل:</b> "
        f"<b>{format_amount(request.from_amount or 0, request.from_currency)}</b>\n\n"
        f"🟢<b>الـمـبـلـغ الـذي سـتـسـتـلـمـه:</b> "
        f"<b>{format_amount(request.to_amount or 0, request.to_currency)}</b>\n\n\n"
        "<b>أدخل عنوان محفظتك أو كود الاستلام في رسالة واحدة.</b>",
        reply_markup=cancel_menu(),
    )
    await state.update_data(
        request_id=request.public_id,
        route_code=route_code.value,
        prompt_chat_id=prompt_message.chat.id,
        prompt_message_id=prompt_message.message_id,
    )

async def _show_target_quote(
    bot: Bot,
    state: FSMContext,
    app_context: ApplicationContext,
    raw_amount: str,
    chat_id: int,
    message_id: int,
    refresh_notice: str | None = None,
) -> None:

    try:
        data = await state.get_data()
        route_code = RouteCode(str(data.get("route_code")))
        mode = str(data.get("quote_mode", ""))
        amount = decimal_from_text(raw_amount)

        if mode not in {"receive", "send"}:
            raise DomainError("طريقة الحساب غير صالحة. ابدأ الحساب من جديد.")
    except (ValueError, DomainError) as error:
        try:
            route_line = request_route_line(RouteCode(str(data.get("route_code"))))
        except (ValueError, UnboundLocalError):
            route_line = ""
        await state.update_data(
            target_amount_input=raw_amount,
            target_quote_ready=False,
        )
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                f"{route_line}"
                f"{html.escape(str(error))}\n\n"
                "أدخل مبلغاً آخر بالأرقام فقط وأرسله."
            ),
            reply_markup=target_calculator_menu(),
        )
        return

    service, session = await service_session(app_context)

    try:
        route = await service.get_route(route_code)
        rates = await service.current_rates()

        if not route.is_enabled:
            raise DomainError("هذا المسار متوقف مؤقتاً.")

        rate = await live_rate_for_route(route_code)
        fee_percentage = app_context.settings.global_fee_percentage

        if route_code in {RouteCode.USD_TO_USDT, RouteCode.USDT_TO_USD}:
            rate_text = "الدولار≈USDT"
        elif route_code in {RouteCode.SYP_TO_USDT, RouteCode.USDT_TO_SYP}:
            rate_text = (
                f"الدولار = "
                f"{format_rate(rate)} ل.س؛ \n"
                "والـUSDT≈الدولار."
            )
        else:
            rate_text = f"1 دولار أمريكي = {format_rate(rate)} ل.س"

        if mode == "send":
            rounded_amount = amount.quantize(
                quantum_for(route.from_currency),
                rounding=ROUND_DOWN,
            )
            if rounded_amount != amount:
                raise DomainError(
                    "المبلغ يحتوي منازل عشرية أكثر مما تسمح به عملة الإرسال."
                )
            amount = rounded_amount

            payment_amount = amount
            net_amount, _ = calculate_route_quote(
                amount=payment_amount,
                route_code=route_code,
                rate=rate,
                fee_percentage=fee_percentage,
                minimum_usd=rates.minimum_usd,
            )

            if route_code in {RouteCode.SYP_TO_USD, RouteCode.SYP_TO_USDT}:
                gross_raw = payment_amount / rate
            elif route_code in {RouteCode.USD_TO_SYP, RouteCode.USDT_TO_SYP}:
                gross_raw = payment_amount * rate
            else:
                gross_raw = payment_amount

            gross_received = gross_raw.quantize(
                quantum_for(route.to_currency),
                rounding=ROUND_DOWN,
            )
            fee_amount = gross_received - net_amount
            amount_context = (
                f"المبلغ الذي حددته للإرسال: "
                f"<b>{format_amount(amount, route.from_currency)}</b>\n\n"
            )
        else:
            (
                payment_amount,
                gross_received,
                fee_amount,
                net_amount,
            ) = calculate_target_breakdown(
                target_amount=amount,
                route_code=route_code,
                rate=rate,
                fee_percentage=fee_percentage,
                minimum_usd=rates.minimum_usd,
                usdt_per_usd=Decimal("1"),
            )
            amount_context = (
                f"المبلغ الذي تريد استلامه صافياً: "
                f"<b>{format_amount(amount, route.to_currency)}</b>\n\n"
            )

        preview_text = (
            "<b>🧾 الـتـفـاصـيـل الـتـقـديـريـة لـلـطـلـب 🧾</b>\n"
            f"{amount_context}"
            f"<b>▫️ سـتـرسـل: </b> "
            f"<b>{format_amount(payment_amount, route.from_currency)}</b>\n\n"
            f"<b>▫️ سعر الصرف: </b> <b>{rate_text}</b>\n\n"
            f"<b>▫️ المبلغ الذي ادخلته: </b> "
            f"<b>{format_amount(gross_received, route.to_currency)}</b>\n\n"
            f"<b>▫️ الـعـمـولـة: </b>  "
            f"({format_rate(fee_percentage)}%): "
            f"<b>{format_amount(fee_amount, route.to_currency)}</b>\n\n"
            f"<b>▫️ صافي ما تستلمه: </b> "
            f"<b>{format_amount(net_amount, route.to_currency)}</b>\n\n\n"
            "<b>● هذا السعر التقديري الحالي بناءً على الأسعار الحالية.</b>\n"
            "<b>● عند المتابعة، سيتم إنشاء طلبك وتثبيت السعر،</b>\n"
            "<b>● وعرض القيم النهائية المثبتة لطلبك.</b>\n"
        )

        
        preview_text = f"{request_route_line(route_code)}{preview_text}"
        
        if refresh_notice:
            preview_text += f"\n\n<i>{html.escape(refresh_notice)}</i>"

        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=preview_text,
            reply_markup=target_calculator_menu(preview=True, mode=mode),
        )
        await state.update_data(
            target_amount_input=raw_amount,
            quote_mode=mode,
            target_quote_ready=True,
            quote_rate_snapshot=str(rate),
        )

    except DomainError as error:
        await state.update_data(
            target_amount_input=raw_amount,
            target_quote_ready=False,
        )
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                f"{request_route_line(route_code)}"
                f"{html.escape(str(error))}\n\n"
                "أدخل مبلغاً آخر وأرسله؛ سيحسبه البوت تلقائياً."
            ),
            reply_markup=target_calculator_menu(
                mode=str((await state.get_data()).get("quote_mode", "receive"))
            ),
        )
    finally:
        await session.close()

@router.callback_query(F.data == "quote:refresh")
async def refresh_target_quote(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        await callback.answer(
            "استخدم البوت في المحادثة الخاصة.",
            show_alert=True,
        )
        return

    data = await state.get_data()
    raw_amount = data.get("target_amount_input")

    if not data.get("target_quote_ready") or not isinstance(raw_amount, str):
        await callback.answer("أدخل المبلغ واحسبه أولاً.", show_alert=True)
        return

    if callback.message.message_id != data.get("prompt_message_id"):
        await callback.answer(
            "هذه شاشة قديمة. ابدأ الحساب من جديد.",
            show_alert=True,
        )
        return

    try:
        route_code = RouteCode(str(data.get("route_code")))
    except ValueError:
        await callback.answer(
            "تعذر استرجاع المسار. ابدأ الحساب من جديد.",
            show_alert=True,
        )
        return

    mode = str(data.get("quote_mode", "receive"))
    previous_rate = data.get("quote_rate_snapshot")

    # نزيل علامة الانتظار فوراً، قبل أي اتصال خارجي.
    await callback.answer("جارٍ تحديث السعر...")

    try:
        refreshed_rate = await live_rate_for_route(
            route_code,
            force_refresh=True,
        )
    except (DomainError, asyncio.TimeoutError):
        await callback.message.answer(
            "تعذر تحديث السعر حالياً؛ بقي العرض السابق كما هو."
        )
        return

    if previous_rate is None:
        notice = "تم جلب السعر وتحديث العرض."
    elif previous_rate == str(refreshed_rate):
        notice = "تم التحديث؛ السعر لم يتغير عن العرض السابق."
    else:
        notice = "تغيّر السعر، وتم تحديث الحساب."

    await _show_target_quote(
        bot=callback.bot,
        state=state,
        app_context=app_context,
        raw_amount=raw_amount,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        refresh_notice=notice,
    )


@router.callback_query(F.data.startswith("quote:mode:"))
async def switch_quote_mode(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        await callback.answer("استخدم البوت في المحادثة الخاصة.", show_alert=True)
        return

    data = await state.get_data()

    current_state = await state.get_state()
    if current_state not in {
        UserFlow.awaiting_quote_mode.state,
        UserFlow.awaiting_target_amount.state,
    }:
        await callback.answer("ابدأ الحساب باختيار مسار التحويل أولاً.", show_alert=True)
        return

    if data.get("editing_target"):
        await callback.answer(
            "لا يمكن تغيير طريقة الحساب أثناء تعديل طلب قائم.",
            show_alert=True,
        )
        return

    mode = (callback.data or "").removeprefix("quote:mode:")
    if mode not in {"receive", "send"}:
        await callback.answer("طريقة حساب غير صالحة.", show_alert=True)
        return

    if (
        callback.message.message_id != data.get("prompt_message_id")
        or callback.message.chat.id != data.get("prompt_chat_id")
    ):
        await callback.answer(
            "هذه شاشة قديمة. ابدأ الحساب من الشاشة الحالية.",
            show_alert=True,
        )
        return

    raw_amount = data.get("target_amount_input")
    try:
        route_code = RouteCode(str(data.get("route_code")))
    except ValueError:
        await callback.answer("تعذر استرجاع مسار التحويل. ابدأ من جديد.", show_alert=True)
        return

    await state.update_data(
        quote_mode=mode,
        target_quote_ready=False,
    )

    if isinstance(raw_amount, str):
        await callback.answer("جارٍ إعادة الحساب...")
        await _show_target_quote(
            bot=callback.bot,
            state=state,
            app_context=app_context,
            raw_amount=raw_amount,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
        )
        return

    await state.update_data(target_amount_input=None)
    await state.set_state(UserFlow.awaiting_target_amount)
    await callback.answer()

    if mode == "send":
        prompt = (
            f"📝<b>الـطـلـب:</b>\n<b>{html.escape(ROUTE_LABELS[route_code])}</b>\n\n"
            "<b>أرسل المبلغ المتوفر لديك للإرسال.</b>\n"
            "سيحسب البوت صافي المبلغ الذي ستستلمه بعد العمولة."
        )
    else:
        prompt = (
            f"📝<b>الـطـلـب:</b>\n<b>{html.escape(ROUTE_LABELS[route_code])}</b>\n\n"
            "<b>أدخل المبلغ الصافي الذي تريد استلامه، بالأرقام فقط.</b>\n"
            "سيحسب البوت المبلغ المطلوب إرساله."
        )

    await callback.message.edit_text(
        prompt,
        reply_markup=target_calculator_menu(mode=mode),
    )

@router.callback_query(F.data == "quote:calculate")
async def calculate_target_quote(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        await callback.answer("استخدم البوت في المحادثة الخاصة.", show_alert=True)
        return

    data = await state.get_data()
    raw_amount = data.get("target_amount_input")
    prompt_message_id = data.get("prompt_message_id")

    if (
        await state.get_state() != UserFlow.awaiting_target_amount.state
        or not isinstance(raw_amount, str)
        or str(data.get("quote_mode", "")) not in {"receive", "send"}
    ):
        await callback.answer("أدخل مبلغ الاستلام أولاً.", show_alert=True)
        return

    if (
        callback.message.message_id != prompt_message_id
        or callback.message.chat.id != data.get("prompt_chat_id")
    ):
        await callback.answer(
            "هذه شاشة قديمة. ابدأ الحساب من الشاشة الحالية.",
            show_alert=True,
        )
        return

    await callback.answer("جارٍ حساب المبلغ...")
    await _show_target_quote(
        bot=callback.bot,
        state=state,
        app_context=app_context,
        raw_amount=raw_amount,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
    )

@router.callback_query(F.data == "quote:change")
async def change_target_quote(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        await callback.answer("استخدم البوت في المحادثة الخاصة.", show_alert=True)
        return

    data = await state.get_data()
    if (
        await state.get_state() != UserFlow.awaiting_target_amount.state
        or callback.message.message_id != data.get("prompt_message_id")
        or callback.message.chat.id != data.get("prompt_chat_id")
    ):
        await callback.answer("هذه شاشة قديمة. ابدأ الحساب من الشاشة الحالية.", show_alert=True)
        return
    mode = str(data.get("quote_mode", "receive"))

    try:
        route_code = RouteCode(str(data.get("route_code")))
    except ValueError:
        await callback.answer("انتهت صلاحية الشاشة. ابدأ الحساب من جديد.", show_alert=True)
        return
    if mode not in {"receive", "send"}:
        await callback.answer("طريقة الحساب غير صالحة. ابدأ الحساب من جديد.", show_alert=True)
        return

    await callback.answer()
    await state.update_data(
        target_amount_input=None,
        target_quote_ready=False,
    )
    
    action_text = "إرسـالـه" if mode == "send" else "اسـتـلامـه"

    prompt = (
        "<b>✏️تــغــيــيــر الــمــبــلــغ✏️</b>\n\n\n"
        f"📝<b>الـطـلـب:</b>\n"
        f"<b>{html.escape(ROUTE_LABELS[route_code])}</b>\n"
        f"<b>● أعِــد إدخـال الـمـبـلـغ الـمـحـدد الـذي تـريـد {action_text} 🖊</b>\n\n"
        "<b>● وسـيـقـوم الـبـوت بـاحـتـسـاب الـقـيـم تـلـقـائـيّـاً 🧮</b>\n\n"
        "<b>● وتـزويـدك بـكـافـة الـتـفـاصـيـل 📝</b>\n"
    )

    await callback.message.edit_text(
        prompt,
        reply_markup=target_calculator_menu(mode=mode),
    )

@router.callback_query(F.data == "quote:continue")
async def continue_with_target_quote(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        await callback.answer("استخدم البوت في المحادثة الخاصة.", show_alert=True)
        return

    lock = _TARGET_QUOTE_LOCKS.setdefault(callback.from_user.id, asyncio.Lock())
    async with lock:
        try:
            await _continue_with_target_quote_locked(callback, state, app_context)
        except Exception:
            data = await state.get_data()
            if data.get("target_quote_processing"):
                await state.update_data(
                    target_quote_ready=True,
                    target_quote_processing=False,
                )
            raise


async def _continue_with_target_quote_locked(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        await callback.answer("استخدم البوت في المحادثة الخاصة.", show_alert=True)
        return

    data = await state.get_data()
    if (
        await state.get_state() != UserFlow.awaiting_target_amount.state
        or callback.message.message_id != data.get("prompt_message_id")
        or callback.message.chat.id != data.get("prompt_chat_id")
    ):
        await callback.answer("احسب المبلغ أولاً.", show_alert=True)
        return
    raw_amount = data.get("target_amount_input")

    if not data.get("target_quote_ready") or not isinstance(raw_amount, str):
        await callback.answer("احسب المبلغ أولاً.", show_alert=True)
        return

    try:
        route_code = RouteCode(str(data.get("route_code")))
    except ValueError:
        await callback.answer("تعذر استرجاع مسار التحويل. ابدأ من جديد.", show_alert=True)
        return

    mode = str(data.get("quote_mode", ""))

    await state.update_data(
        target_quote_ready=False,
        target_quote_processing=True,
    )
    await callback.answer("جارٍ تجهيز الطلب...")

    service, session = await service_session(app_context)
    try:
        draft_arguments = dict(
            route_code=route_code,
            user_id=callback.from_user.id,
            chat_id=callback.message.chat.id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name,
        )

        if mode == "send":
            request = await service.create_send_amount_draft(
                **draft_arguments,
                raw_send_amount=raw_amount,
            )
        elif mode == "receive":
            request = await service.create_target_draft(
                **draft_arguments,
                raw_target_amount=raw_amount,
            )
        else:
            raise DomainError("طريقة الحساب غير صالحة. ابدأ الحساب من جديد.")
        await state.update_data(target_quote_processing=False)

    except DomainError as error:
        await state.update_data(
            target_quote_ready=False,
            target_quote_processing=False,
        )
        await callback.message.edit_text(
            f"{request_route_line(route_code)}"
            f"{html.escape(str(error))}\n\n"
            "يمكنك تغيير المبلغ ثم المتابعة.",
            reply_markup=target_calculator_menu(
                mode=str(data.get("quote_mode", "receive"))
            ),
        )
        return

    finally:
        await session.close()

    await state.update_data(
        request_id=request.public_id,
        route_code=route_code.value,
        prompt_chat_id=callback.message.chat.id,
        prompt_message_id=callback.message.message_id,
        target_quote_ready=False,
        target_quote_processing=False,
    )
   
    confirmed_text = (
        f"تم تثبيت الطلب رقم <b>#{hcode(request.public_id)}</b>.\n\n"
        f"{request_route_line(request.route_code)}"
        f"سترسل: <b>{format_amount(request.from_amount or 0, request.from_currency)}</b>\n"
        f"صافي الاستلام: <b>{format_amount(request.to_amount or 0, request.to_currency)}</b>\n"
        f"العمولة: <b>{format_rate(request.fee_percentage_snapshot)}%</b>\n"
        f"سعر الصرف المثبّت: <b>{format_rate(request.rate_snapshot)}</b>\n\n"
    )

    if request.to_currency is Currency.USDT:
        await state.set_state(UserFlow.awaiting_crypto_method)
        await callback.message.edit_text(
            confirmed_text
            + "<b>اختر نوع محفظة USDT التي تريد الاستلام عليها.</b>",
            reply_markup=user_crypto_method_menu(),
        )
        return

    await state.set_state(UserFlow.awaiting_payout_address)
    await callback.message.edit_text(
        confirmed_text + "أدخل عنوان الاستلام في رسالة واحدة.",
        reply_markup=cancel_menu(),
    )

@router.message(UserFlow.awaiting_reference, F.text)
async def receive_reference(
    message: Message,
    state: FSMContext,
    app_context: ApplicationContext,
    bot: Bot,
) -> None:

    if not message.from_user:
        return
    data = await state.get_data()
    request_id = data.get("request_id")
    if not isinstance(request_id, str):
        await state.clear()
        await message.answer("انتهت الجلسة؛ ابدأ طلباً جديداً من /start.")
        return
    service, session = await service_session(app_context)
    try:
        try:
            route_line = request_route_line(RouteCode(str(data.get("route_code"))))
        except ValueError:
            route_line = ""
        request = await service.set_payment_reference(
            request_id,
            message.from_user.id,
            message.text or "",
        )

        try:
            await message.delete()
        except TelegramAPIError:
            logger.warning("حاول لاحقاً.")

        if request.status is RequestStatus.PENDING_VERIFICATION:
            expiry_task = _EXPIRY_TASKS_BY_REQUEST.pop(request_id, None)
            if expiry_task is not None:
                expiry_task.cancel()

            barcode_chat_id = data.get("barcode_chat_id")
            barcode_message_id = data.get("barcode_message_id")

            if isinstance(barcode_chat_id, int) and isinstance(barcode_message_id, int):
                try:
                    await bot.delete_message(
                        chat_id=barcode_chat_id,
                        message_id=barcode_message_id,
                    )
                except TelegramAPIError:
                    logger.warning(
                        "حاول لاحقاً %s.",
                        request_id,
                    )

            await state.clear()

            await bot.send_message(
                chat_id=message.chat.id,
                text=(
                    "✅ <b>تم الإبلاغ عن الدفع — بانتظار التحقق من الإدارة</b>\n\n"
                    f"رقم الطلب: <b>#{hcode(request.public_id)}</b>\n"
                    f"{request_route_line(request.route_code)}"
                    f"مبلغ الدفع: "
                    f"<b>{format_amount(request.from_amount or 0, request.from_currency)}</b>\n"
                    f"مبلغ الاستلام: "
                    f"<b>{format_amount(request.to_amount or 0, request.to_currency)}</b>\n"
                    f"عنوان الاستلام: {hcode(request.payout_address or '')}\n"
                    f"مرجع الدفع: {hcode(request.payment_reference or '')}\n\n"
                    f"مدة المعالجة المتوقعة: "
                    f"<b>{html.escape(request.eta_snapshot)}</b>\n\n"
                    "لمراجعة هذا الطلب لاحقاً، اضغط «طلباتي»."
                ),
                reply_markup=main_menu(
                    app_context.is_admin(message.from_user.id),
                    app_context.settings.contact_url,
                ),
            )

            await notify_admins(bot, app_context, request)

        elif data.get("editing"):
        
            await state.update_data(editing=False)
            await state.set_state(UserFlow.awaiting_amount)
            try:
                await bot.delete_message(
                    chat_id=data["prompt_chat_id"],
                    message_id=data["prompt_message_id"],
                )
            except TelegramAPIError:
                logger.warning("حاول لاحقاً.")

            prompt_message = await message.answer(
                f"{request_route_line(request.route_code)}"
                "<b>✏️تـعـديـل بـيـانـاتـك✏️:\n\n● أعِد إدخال المبلغ الذي تريد أن تـسـتـلـمـه،</b>\n\nسيحسب البوت تلقائياً المبلغ الذي سترسله.\n\n"
                "أرسل أرقاماً فقط، مثال: <code>50</code> أو <code>1517</code>  إلخ إلخ ..\n\n✏️",
                reply_markup=cancel_menu(),
            )
            await state.update_data(
                prompt_chat_id=prompt_message.chat.id,
                prompt_message_id=prompt_message.message_id,
            )

        else:
            barcode_chat_id = data.get("barcode_chat_id")
            barcode_message_id = data.get("barcode_message_id")

            if isinstance(barcode_chat_id, int) and isinstance(barcode_message_id, int):
                try:
                    await bot.delete_message(
                        chat_id=barcode_chat_id,
                        message_id=barcode_message_id,
                    )
                except TelegramAPIError:
                    logger.warning("حاول لاحقاً.")

            await state.set_state(UserFlow.awaiting_amount)
            prompt_message = await message.answer(
                f"{request_route_line(request.route_code)}"
                "<b>أرسـل الـقـيـمـة الـتـي حـوّلـتـهـا.</b>\n\n"
                "أرسل أرقاماً فقط، مثال: <code>50</code> أو <code>1517</code>  إلخ إلخ ..",
                reply_markup=cancel_menu(),
            )
            await state.update_data(
                prompt_chat_id=prompt_message.chat.id,
                prompt_message_id=prompt_message.message_id,
            )
    except DuplicatePaymentReferenceError as error:
        try:
            await message.delete()
        except TelegramAPIError:
            logger.warning("حاول لاحقاً.")

        barcode_chat_id = data.get("barcode_chat_id")
        barcode_message_id = data.get("barcode_message_id")
        barcode_caption = data.get("barcode_caption")

        if (
            isinstance(barcode_chat_id, int)
            and isinstance(barcode_message_id, int)
            and isinstance(barcode_caption, str)
        ):
            try:
                await bot.edit_message_caption(
                    chat_id=barcode_chat_id,
                    message_id=barcode_message_id,
                    caption=(
                        f"{barcode_caption}\n\n"
                        f"<b>⚠️ {html.escape(str(error))}</b>"
                    ),
                )
            except TelegramBadRequest as edit_error:
                if "message is not modified" not in str(edit_error):
                    raise
        else:
            await message.answer(
                f"{route_line}{html.escape(str(error))}",
                reply_markup=cancel_menu(),
            )


    except DomainError as error:
        await message.answer(html.escape(str(error)), reply_markup=cancel_menu())
    finally:
        await session.close()

@router.callback_query(F.data.startswith("crypto:choose:"))
async def choose_user_crypto_method(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
    bot: Bot,
) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        await callback.answer("استخدم البوت في المحادثة الخاصة.", show_alert=True)
        return

    method = (callback.data or "").removeprefix("crypto:choose:")
    if method not in CRYPTO_METHOD_LABELS:
        await callback.answer("طريقة USDT غير صالحة.", show_alert=True)
        return

    if await state.get_state() != UserFlow.awaiting_crypto_method.state:
        await callback.answer(
            "انتهت صلاحية الاختيار. ابدأ طلباً جديداً.",
            show_alert=True,
        )
        return

    data = await state.get_data()
    if (
        callback.message.message_id != data.get("prompt_message_id")
        or callback.message.chat.id != data.get("prompt_chat_id")
    ):
        await callback.answer("هذه شاشة قديمة. افتح شاشة الطلب الحالية.", show_alert=True)
        return

    try:
        route_code = RouteCode(str(data.get("route_code")))
    except ValueError:
        await callback.answer(
            "تعذر استرجاع مسار الطلب. ابدأ من جديد.",
            show_alert=True,
        )
        return

    payout_routes = {
        RouteCode.SYP_TO_USDT,
        RouteCode.USD_TO_USDT,
    }
    payment_routes = {
        RouteCode.USDT_TO_SYP,
        RouteCode.USDT_TO_USD,
    }

    if route_code in payout_routes:
        await state.update_data(crypto_method=method)
        await state.set_state(UserFlow.awaiting_payout_address)

        current_text = callback.message.text or ""
        confirmation_text = current_text.split(
            "\n\n<b>اختر نوع محفظة USDT",
            maxsplit=1,
        )[0]

        if method == "CWALLET":
            address_prompt = (
                "أرسل بريدك الإلكتروني أو رقم حساب Cwallet "
                "الذي تريد استلام USDT عليه."
            )
        elif method == "BINANCE":
            address_prompt = (
                "أرسل بريدك الإلكتروني أو رقم حساب Binance "
                "الذي تريد استلام USDT عليه."
            )
        else:
            address_prompt = (
                f"أرسل عنوان محفظة USDT على خيار "
                f"<b>{html.escape(CRYPTO_METHOD_LABELS[method])}</b>."
            )

        await callback.answer()
        await callback.message.edit_text(
            f"{confirmation_text}\n\n"
            f"طريقة الاستلام المختارة: "
            f"<b>{html.escape(CRYPTO_METHOD_LABELS[method])}</b>\n"
            f"{address_prompt}",
            reply_markup=cancel_menu(),
        )
        return

    if route_code not in payment_routes:
        await callback.answer(
            "طريقة USDT غير متاحة لهذا المسار.",
            show_alert=True,
        )
        return

    request_id = data.get("request_id")
    payout_address = data.get("payout_address_input")

    if not isinstance(request_id, str) or not isinstance(payout_address, str):
        await callback.answer(
            "بيانات الطلب ناقصة. ابدأ طلباً جديداً.",
            show_alert=True,
        )
        return

    await callback.answer("جارٍ تجهيز المراجعة...")
    service, session = await service_session(app_context)

    try:
        request = await service.prepare_user_review(
            request_id,
            callback.from_user.id,
            payout_address,
            crypto_method=method,
        )
        await present_crypto_request_review(
            bot=bot,
            service=service,
            state=state,
            app_context=app_context,
            request=request,
            user_id=callback.from_user.id,
            chat_id=callback.message.chat.id,
            edit_message=callback.message,
        )
    except DomainError as error:
        await state.update_data(
            crypto_method=None,
            payout_address_input=None,
        )
        await state.set_state(UserFlow.awaiting_payout_address)
        await callback.message.edit_text(
            f"{request_route_line(route_code)}"
            f"{html.escape(str(error))}\n\n"
            "أعد إرسال عنوان استلام شام كاش في رسالة واحدة.",
            reply_markup=cancel_menu(),
        )
    finally:
        await session.close()

@router.message(UserFlow.awaiting_amount, F.text)
async def receive_amount(message: Message, state: FSMContext, app_context: ApplicationContext, bot: Bot) -> None:
    if not message.from_user:
        return
    data = await state.get_data()
    request_id = data.get("request_id")
    if not isinstance(request_id, str):
        await state.clear()
        await message.answer("انتهت الجلسة؛ ابدأ طلباً جديداً من /start.")
        return
    service, session = await service_session(app_context)
    try:
        request = await service.set_amount(request_id, message.from_user.id, message.text or "")
        await state.set_state(UserFlow.awaiting_payout_address)
        try:
            await message.delete()
        except TelegramAPIError:
            logger.warning("حاول لاحقاً.")

        try:
            await bot.delete_message(
                chat_id=data["prompt_chat_id"],
                message_id=data["prompt_message_id"],
            )
        except TelegramAPIError:
            logger.warning("حاول لاحقاً.")

        prompt_message = await message.answer(
            f"{request_route_line(request.route_code)}"
            f"سيصلك: <b>{format_amount(request.to_amount or 0, request.to_currency)}</b>\n\n"
            "<b>أدخل عنوان محفظتك أو كود الاستلام الخاص بك</b> في رسالة واحدة.",
            reply_markup=cancel_menu(),
        )
        await state.update_data(
            prompt_chat_id=prompt_message.chat.id,
            prompt_message_id=prompt_message.message_id,
        )

    except DomainError as error:
        try:
            await bot.delete_message(
                chat_id=data["prompt_chat_id"],
                message_id=data["prompt_message_id"],
            )
        except TelegramAPIError:
            logger.warning("حاول لاحقاً.")

        prompt_message = await message.answer(
            f"{html.escape(str(error))}\n\nأعد إدخال المبلغ الصحيح ثم أرسل الرقم.",
            reply_markup=cancel_menu(),
        )
        await state.update_data(
            prompt_chat_id=prompt_message.chat.id,
            prompt_message_id=prompt_message.message_id,
        )

    finally:
        await session.close()


@router.message(UserFlow.awaiting_payout_address, F.text)
async def receive_payout_address(
    message: Message,
    state: FSMContext,
    app_context: ApplicationContext,
    bot: Bot,
) -> None:
    if not message.from_user:
        return

    data = await state.get_data()
    request_id = data.get("request_id")

    if not isinstance(request_id, str):
        await state.clear()
        await message.answer("انتهت الجلسة؛ ابدأ طلباً جديداً من /start.")
        return

    try:
        route_code = RouteCode(str(data.get("route_code")))
    except ValueError:
        await state.clear()
        await message.answer("تعذر استرجاع مسار الطلب. ابدأ من جديد.")
        return

    payment_routes = {
        RouteCode.USDT_TO_SYP,
        RouteCode.USDT_TO_USD,
    }

    # في مسار دفع USDT: نحفظ عنوان شام كاش مؤقتاً،
    # ثم نطلب من المستخدم اختيار طريقة إرسال USDT.
    if route_code in payment_routes:
        payout_address_input = (message.text or "").strip()
        if not payout_address_input:
            await message.answer(
                f"{request_route_line(route_code)}"
                "أرسل عنوان استلام شام كاش في رسالة واحدة.",
                reply_markup=cancel_menu(),
            )
            return

        prompt_chat_id = data.get("prompt_chat_id")
        prompt_message_id = data.get("prompt_message_id")

        await state.update_data(
            payout_address_input=payout_address_input,
            crypto_method=None,
        )
        await state.set_state(UserFlow.awaiting_crypto_method)

        try:
            await message.delete()
        except TelegramAPIError:
            logger.info("تعذر حذف رسالة عنوان الاستلام.")

        if isinstance(prompt_chat_id, int) and isinstance(prompt_message_id, int):
            await bot.edit_message_text(
                chat_id=prompt_chat_id,
                message_id=prompt_message_id,
                text=(
                    f"تم استلام عنوان استلام شام كاش:\n"
                    f"{request_route_line(route_code)}"
                    f"<code>{html.escape(payout_address_input)}</code>\n\n"
                    "<b>اختر طريقة إرسال USDT:</b>"
                ),
                reply_markup=user_crypto_method_menu(),
            )

        else:
            prompt_message = await message.answer(
                f"{request_route_line(route_code)}"
                "<b>اختر طريقة إرسال USDT:</b>",
                reply_markup=user_crypto_method_menu(),
            )
            
            await state.update_data(
                prompt_chat_id=prompt_message.chat.id,
                prompt_message_id=prompt_message.message_id,
            )
        return

    # في مسار استلام USDT، يجب أن يكون اختيار طريقة الاستلام قد تم أولاً.
    crypto_method = data.get("crypto_method")
    if route_code in {RouteCode.SYP_TO_USDT, RouteCode.USD_TO_USDT}:
        if not isinstance(crypto_method, str) or crypto_method not in CRYPTO_METHOD_LABELS:
            await state.set_state(UserFlow.awaiting_crypto_method)
            prompt_chat_id = data.get("prompt_chat_id")
            prompt_message_id = data.get("prompt_message_id")
            if isinstance(prompt_chat_id, int) and isinstance(prompt_message_id, int):
                await bot.edit_message_text(
                    chat_id=prompt_chat_id,
                    message_id=prompt_message_id,
                    text=(
                        f"{request_route_line(route_code)}"
                        "<b>اختر طريقة استلام USDT أولاً:</b>"
                    ),
                    reply_markup=user_crypto_method_menu(),
                )
            return

    service, session = await service_session(app_context)

    try:
        request = await service.prepare_user_review(
            request_id,
            message.from_user.id,
            message.text or "",
            crypto_method=crypto_method if isinstance(crypto_method, str) else None,
        )

        try:
            await message.delete()
        except TelegramAPIError:
            logger.info("تعذر حذف رسالة عنوان الاستلام.")

        prompt_chat_id = data.get("prompt_chat_id")
        prompt_message_id = data.get("prompt_message_id")

        if isinstance(prompt_chat_id, int) and isinstance(prompt_message_id, int):
            try:
                await bot.delete_message(
                    chat_id=prompt_chat_id,
                    message_id=prompt_message_id,
                )
            except TelegramAPIError:
                logger.info("تعذر حذف رسالة طلب عنوان الاستلام.")

        await present_crypto_request_review(
            bot=bot,
            service=service,
            state=state,
            app_context=app_context,
            request=request,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
        )

    except DomainError as error:
        try:
            await message.delete()
        except TelegramAPIError:
            logger.info("تعذر حذف رسالة الإدخال غير الصالحة.")

        prompt_message = await message.answer(
            f"{request_route_line(route_code)}"
            f"{html.escape(str(error))}\n\n"
            "أعد إدخال عنوان الاستلام في رسالة واحدة.",
            reply_markup=cancel_menu(),
        )
        await state.set_state(UserFlow.awaiting_payout_address)
        await state.update_data(
            prompt_chat_id=prompt_message.chat.id,
            prompt_message_id=prompt_message.message_id,
        )
    finally:
        await session.close()

@router.callback_query(F.data == "request:confirm")
async def confirm_user_request(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
    bot: Bot,
) -> None:
    if not callback.from_user:
        await _confirm_user_request_locked(callback, state, app_context, bot)
        return
    lock = _USER_REVIEW_LOCKS.setdefault(callback.from_user.id, asyncio.Lock())
    async with lock:
        await _confirm_user_request_locked(callback, state, app_context, bot)


async def _confirm_user_request_locked(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
    bot: Bot,
) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        await callback.answer("استخدم البوت في المحادثة الخاصة.", show_alert=True)
        return

    data = await state.get_data()
    if not await is_current_user_review(callback, state, data):
        await callback.answer("انتهت صلاحية الطلب. ابدأ طلباً جديداً.", show_alert=True)
        return
    request_id = data.get("request_id")
    if not isinstance(request_id, str):
        await callback.answer("انتهت صلاحية الطلب. ابدأ طلباً جديداً.", show_alert=True)
        return

    service, session = await service_session(app_context)
    try:
        request = await service.confirm_user_review(
            request_id,
            callback.from_user.id,
        )
    except DomainError as error:
        await callback.answer(str(error), show_alert=True)
        return
    finally:
        await session.close()
        
    cancel_expiry_task(request.public_id)

    barcode_source = request.barcode_file_id_snapshot
    if not barcode_source:
        await callback.answer(
            "حاول لاحقاً.",
            show_alert=True,
        )
        return

    barcode_path = Path(barcode_source)
    barcode_input = (
        FSInputFile(barcode_path)
        if barcode_path.is_file()
        else barcode_source
    )

    if request.from_currency is Currency.USDT and request.crypto_method in {
        "CWALLET",
        "BINANCE",
    }:
        reference_instruction = (
            "بعد التحويل المباشر، أرسل هنا رقم العملية أو صورة/PDF لإثبات الدفع."
        )
    elif request.from_currency is Currency.USDT:
        reference_instruction = (
            f"بعد الدفع عبر {CRYPTO_METHOD_LABELS.get(request.crypto_method, 'شبكة USDT')}, "
            "أرسل هنا TxID أو صورة/PDF لإثبات الدفع."
        )
    else:
        reference_instruction = (
            "بعد الدفع، أرسل هنا رقم العملية أو صورة/PDF لإثبات الدفع."
       )

    crypto_method_line = ""
    if request.crypto_method in CRYPTO_METHOD_LABELS:
        crypto_method_line = (
            f"💱<b>طريقة USDT المختارة:</b> "
            f"<b>{html.escape(CRYPTO_METHOD_LABELS[request.crypto_method])}</b>\n\n"
        )

        
    payment_caption = (
        f"📋<b>طــلــب #{hcode(request.public_id)} — بانتظار الدفع</b>\n\n\n"
        f"📝<b>الـطـلـب:</b>\n<b>{html.escape(ROUTE_LABELS[request.route_code])}</b>\n\n"
        f"📤<b>مبلغ الدفع المطلوب: </b>"
        f"<b>{format_amount(request.from_amount or 0, request.from_currency)}</b>\n\n"
        f"📥<b>مبلغ الإستلام: </b>"
        f"<b>{format_amount(request.to_amount or 0, request.to_currency)}</b>\n\n"
        f"🪪<b>عـنـوان الإسـتـلام:</b>\n{hcode(request.payout_address or '')}\n\n"
        f"💳<b>عـنـوان الـدفـع:</b>\n{hcode(request.payment_address_snapshot)}\n\n"
        f"⏱️ بيانات الدفع والباركود صالحان لمدة "
        f"<b>{app_context.settings.quote_ttl_minutes} دقيقة</b> من وقت ظهورهما.⏳\n\n"
        "<blockquote expandable>"
        "<tg-spoiler>⚠️ <b>لا تـسـتخـدم بـيـانـات الـدفـع بـعـد انـتـهـاء الـمـهـلـة.</b></tg-spoiler>\n\n"
        "<tg-spoiler>🔃 <b>لإن بـيـانـات الـدفـع مـتـغـيـرة بـاسـتـمـرار.</b></tg-spoiler>\n\n"
        "<tg-spoiler>⛔ <b>فـاسـتـخـدامـهـا قـد يـؤدي لـخـسـارة أمـوالـك.</b></tg-spoiler>\n\n"
        "<tg-spoiler>🆕 <b>لـذلـك يـمـكـنـك إنـشـاء طـلـب جـديـد.</b></tg-spoiler>\n"
        "</blockquote>\n\n"
        f"{reference_instruction}"
    )

    await callback.answer("تم تأكيد الطلب. تظهر الآن بيانات الدفع.")

    try:
        await callback.message.delete()
    except TelegramAPIError:
        logger.warning("حاول لاحقاً %s.", request.public_id)

    try:
        payment_message = await bot.send_photo(
            chat_id=callback.message.chat.id,
            photo=barcode_input,
            caption=payment_caption,
            reply_markup=payment_menu(request.payment_address_snapshot),
        )
    except TelegramAPIError:
        service, session = await service_session(app_context)
        try:
            await service.cancel_draft(request.public_id, callback.from_user.id)
        finally:
            await session.close()
        await callback.message.answer("حاول لاحقاً.")
        return

    service, session = await service_session(app_context)
    try:
        expires_at = await service.activate_payment_expiry(
            request.public_id,
            callback.from_user.id,
            payment_message.message_id,
        )
    except DomainError as error:
        try:
            await bot.delete_message(callback.message.chat.id, payment_message.message_id)
        except TelegramAPIError:
            logger.info("حاول لاحقاً.")
        await service.cancel_draft(request.public_id, callback.from_user.id)
        await callback.message.answer(html.escape(str(error)))
        return
    finally:
        await session.close()

    await state.set_state(UserFlow.awaiting_reference)
    await state.update_data(
        request_id=request.public_id,
        barcode_chat_id=payment_message.chat.id,
        barcode_message_id=payment_message.message_id,
        barcode_caption=payment_caption,
        prompt_chat_id=payment_message.chat.id,
        prompt_message_id=payment_message.message_id,
        editing=False,
    )

    expiry_task = asyncio.create_task(
        expire_reference_barcode(
            bot=bot,
            app_context=app_context,
            state=state,
            user_id=callback.from_user.id,
            chat_id=payment_message.chat.id,
            request_id=request.public_id,
            barcode_message_id=payment_message.message_id,
            expires_at=expires_at,
        )
    )
    track_expiry_task(request.public_id, expiry_task)

@router.callback_query(F.data == "request:edit")
async def edit_user_request(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.from_user:
        await _edit_user_request_locked(callback, state, app_context)
        return
    lock = _USER_REVIEW_LOCKS.setdefault(callback.from_user.id, asyncio.Lock())
    async with lock:
        await _edit_user_request_locked(callback, state, app_context)


async def _edit_user_request_locked(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        await callback.answer("استخدم البوت في المحادثة الخاصة.", show_alert=True)
        return

    data = await state.get_data()
    if not await is_current_user_review(callback, state, data):
        await callback.answer("انتهت صلاحية الطلب. ابدأ طلباً جديداً.", show_alert=True)
        return
    request_id = data.get("request_id")
    if not isinstance(request_id, str):
        await callback.answer("انتهت صلاحية الطلب. ابدأ طلباً جديداً.", show_alert=True)
        return

    service, session = await service_session(app_context)
    try:
        await service.restart_draft_for_edit(request_id, callback.from_user.id)
    except DomainError as error:
        await callback.answer(str(error), show_alert=True)
        return
    finally:
        await session.close()

    cancel_expiry_task(request_id)

    mode = str(data.get("quote_mode", "receive"))
    if mode not in {"send", "receive"}:
        mode = "receive"
    await state.set_state(UserFlow.awaiting_target_amount)
    await state.update_data(
        request_id=request_id,
        editing_target=True,
        route_code=data.get("route_code"),
        quote_mode=mode,
    )
    await callback.answer()
    
    try:
        await callback.message.delete()
    except TelegramAPIError:
        logger.warning("حاول لاحقاً.")
    prompt_message = await callback.message.answer(
        f"{request_route_line(RouteCode(str(data.get('route_code'))))}"
        "<b>✏️تـعـديـل بـيـانـاتـك✏️:</b>\n\n"
        f"{'أرسل المبلغ المتوفر لديك للإرسال، وسيُعاد حساب صافي الاستلام تلقائياً.' if mode == 'send' else 'أرسل صافي المبلغ الذي تريد استلامه، وسيُعاد حساب مبلغ الإرسال تلقائياً.'}",
        reply_markup=target_calculator_menu(mode=mode, show_modes=False),
    )
    await state.update_data(
        prompt_chat_id=prompt_message.chat.id,
        prompt_message_id=prompt_message.message_id,
    )

@router.callback_query(F.data == "request:cancel")
async def cancel_confirmed_draft(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.from_user:
        await _cancel_confirmed_draft_locked(callback, state, app_context)
        return
    lock = _USER_REVIEW_LOCKS.setdefault(callback.from_user.id, asyncio.Lock())
    async with lock:
        await _cancel_confirmed_draft_locked(callback, state, app_context)


async def _cancel_confirmed_draft_locked(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        await callback.answer("استخدم البوت في المحادثة الخاصة.", show_alert=True)
        return

    data = await state.get_data()
    if not await is_current_user_review(callback, state, data):
        await callback.answer("انتهت صلاحية الطلب.", show_alert=True)
        return
    request_id = data.get("request_id")
    if not isinstance(request_id, str):
        await callback.answer("انتهت صلاحية الطلب.", show_alert=True)
        return

    service, session = await service_session(app_context)
    try:
        await service.cancel_draft(request_id, callback.from_user.id)
    except DomainError as error:
        await callback.answer(str(error), show_alert=True)
        return
    finally:
        await session.close()

    cancel_expiry_task(request_id)
    
    await state.clear()
    await callback.answer("تم إلغاء الطلب.")
    try:
        await callback.message.delete()
    except TelegramAPIError:
        logger.warning("حاول لاحقاً.")

    await show_home(callback.message, app_context)
    

@router.message(UserFlow.awaiting_reference, F.photo | F.document)
async def receive_payment_proof(
    message: Message,
    state: FSMContext,
    app_context: ApplicationContext,
    bot: Bot,
) -> None:
    if not message.from_user:
        return

    data = await state.get_data()
    try:
        route_line = request_route_line(RouteCode(str(data.get("route_code"))))
    except ValueError:
        route_line = ""

    if message.photo:
        photo = message.photo[-1]
        file_id = photo.file_id
        file_size = photo.file_size
        if file_size is None:
            try:
                file_size = (await bot.get_file(file_id)).file_size
            except TelegramAPIError:
                await message.answer(f"{route_line}حاول لاحقاً.")
                return
        if not payment_proof_file_size_allowed(file_size):
            await message.answer(f"{route_line}حجم الإثبات يتجاوز 20 ميغابايت. أرسل صورة أو PDF أصغر.")
            return
        file_type = "photo"
    elif message.document:
        document = message.document
        is_pdf = (
            document.mime_type == "application/pdf"
            or (document.file_name or "").lower().endswith(".pdf")
        )
        if not is_pdf:
            await message.answer(
                f"{route_line}الملف غير مدعوم. أرسل صورة أو ملف PDF لإثبات الدفع."
            )
            return
        if document.file_size is not None and not payment_proof_file_size_allowed(document.file_size):
            await message.answer(f"{route_line}حجم الإثبات يتجاوز 20 ميغابايت. أرسل صورة أو PDF أصغر.")
            return
        try:
            telegram_file = await bot.get_file(document.file_id)
        except TelegramAPIError:
            await message.answer(f"{route_line}حاول لاحقاً.")
            return
        file_size = document.file_size if document.file_size is not None else telegram_file.file_size
        if not payment_proof_file_size_allowed(file_size):
            await message.answer(f"{route_line}حجم الإثبات يتجاوز 20 ميغابايت. أرسل صورة أو PDF أصغر.")
            return
        if not telegram_file.file_path:
            await message.answer(f"{route_line}حاول لاحقاً.")
            return
        content = io.BytesIO()
        try:
            await bot.download_file(telegram_file.file_path, destination=content)
        except TelegramAPIError:
            await message.answer(f"{route_line}حاول لاحقاً.")
            return
        if not payment_proof_pdf_signature_valid(content.getvalue()):
            await message.answer(
                f"{route_line}الملف غير مدعوم. أرسل صورة أو ملف PDF لإثبات الدفع."
            )
            return
        file_id = document.file_id
        file_type = "pdf"
    else:
        return

    request_id = data.get("request_id")
    if not isinstance(request_id, str):
        await state.clear()
        await message.answer("انتهت الجلسة؛ ابدأ طلباً جديداً من /start.")
        return

    service, session = await service_session(app_context)
    try:
        request = await service.set_payment_proof(
            request_id,
            message.from_user.id,
            file_id,
            file_type,
        )
    except DomainError as error:
        await message.answer(
            f"{route_line}{html.escape(str(error))}",
            reply_markup=cancel_menu(),
        )
        return
    finally:
        await session.close()

    if request.status is not RequestStatus.PENDING_VERIFICATION:
        await message.answer(f"{route_line}تعذر تسجيل إثبات الدفع. تواصل مع الإدارة.")
        return

    expiry_task = _EXPIRY_TASKS_BY_REQUEST.pop(request_id, None)
    if expiry_task is not None:
        expiry_task.cancel()

    barcode_chat_id = data.get("barcode_chat_id")
    barcode_message_id = data.get("barcode_message_id")
    if isinstance(barcode_chat_id, int) and isinstance(barcode_message_id, int):
        try:
            await bot.delete_message(
                chat_id=barcode_chat_id,
                message_id=barcode_message_id,
            )
        except TelegramAPIError as error:
            logger.warning(
                "تعذر حذف رسالة الدفع للطلب %s: %s",
                request_id,
                error,
            )

    await state.clear()

    await message.answer(
        "✅ وصل إثبات الدفع، والطلب بانتظار مراجعة الإدارة.\n"
        f"{request_route_line(request.route_code)}"
        f"رقم الطلب: <b>#{hcode(request.public_id)}</b>\n"
        "سنبلغك عند تحديث حالته.",
        reply_markup=main_menu(
            app_context.is_admin(message.from_user.id),
            app_context.settings.contact_url,
        ),
    )

    await notify_admins(bot, app_context, request)

@router.message(UserFlow.awaiting_reference)
async def invalid_reference(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        route_line = request_route_line(RouteCode(str(data.get("route_code"))))
    except ValueError:
        route_line = ""
    await message.answer(
        f"{route_line}أرسل رقم العملية كنص أو صورة/PDF لإثبات الدفع، أو ألغِ الطلب.",
        reply_markup=cancel_menu(),
    )


@router.message(UserFlow.awaiting_amount)
async def invalid_amount(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        route_line = request_route_line(RouteCode(str(data.get("route_code"))))
    except ValueError:
        route_line = ""
    await message.answer(
        f"{route_line}أرسل المبلغ المطلوب بالأرقام فقط، وبحد أقصى منزلتين عشريتين للدولار أو USDT.",
        reply_markup=cancel_menu(),
    )


@router.message(UserFlow.awaiting_payout_address)
async def invalid_address(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        route_line = request_route_line(RouteCode(str(data.get("route_code"))))
    except ValueError:
        route_line = ""
    await message.answer(
        f"{route_line}أرسل عنوان الاستلام المطلوب في هذه الخطوة، أو استخدم الأزرار الظاهرة على الشاشة.",
        reply_markup=cancel_menu(),
    )

@router.callback_query(F.data == "flow:cancel")
async def cancel_flow(callback: CallbackQuery, state: FSMContext, app_context: ApplicationContext) -> None:
    if not callback.message or not callback.from_user:
        return
    data = await state.get_data()
    prompt_chat_id = data.get("prompt_chat_id")
    prompt_message_id = data.get("prompt_message_id")
    if (
        isinstance(prompt_chat_id, int)
        and isinstance(prompt_message_id, int)
        and (
            callback.message.chat.id != prompt_chat_id
            or callback.message.message_id != prompt_message_id
        )
    ):
        await callback.answer("هذه شاشة قديمة؛ لم يتم إلغاء الطلب الحالي.", show_alert=True)
        return
    request_id = data.get("request_id")
    if isinstance(request_id, str):
        service, session = await service_session(app_context)
        try:
            await service.cancel_draft(request_id, callback.from_user.id)
        except DomainError:
            pass
        finally:
            await session.close()
    if isinstance(request_id, str):
        expiry_task = _EXPIRY_TASKS_BY_REQUEST.pop(request_id, None)
        if expiry_task is not None:
            expiry_task.cancel()
    await state.clear()
    await callback.answer("أُلغي الطلب.")
    try:
        await callback.message.delete()
    except TelegramAPIError:
        logger.warning("حاول لاحقاً.")


    await show_home(callback.message, app_context)


@router.callback_query(F.data == "menu:requests")
async def my_requests(callback: CallbackQuery, app_context: ApplicationContext) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        return
    service, session = await service_session(app_context)
    try:
        requests = await service.user_requests(callback.from_user.id)
        text = "لا توجد طلبات سابقة." if not requests else "<b>آخر طلباتك</b>\n\n" + "\n\n".join(
            request_summary(request) for request in requests
        )
        await callback.answer()
        try:
            await callback.message.edit_text(text, reply_markup=main_menu(app_context.is_admin(callback.from_user.id), app_context.settings.contact_url))
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error):
                raise
    finally:
            await session.close()


@router.callback_query(F.data == "menu:referrals")
async def referrals_callback(callback: CallbackQuery) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        return
    await callback.answer()
    await callback.message.edit_text(
               "<b>💲 الـعـمـل و الـكـسـب الآمـن 💯</b>\n\n\n"
        "مرحباً بك في نظام الكسب الرقمي الموثق والآمن من:\n<b>✅الـوسـاطـة الـرقـمـيـة الـسـوريـة✅</b>\n\n"
        "<b>💡 كيف تبدأ بجني الأرباح الحقيقية؟</b>\n\n"
        "1️⃣ انسخ رابط الدعوة المعتمد الخاص بك من الزر أدناه.\n"
        "2️⃣ شاركه مع أصدقائك وعلى منصات التواصل (فيسبوك، انستغرام، واتساب).\n"
        "3️⃣ تلقَّ مكافأتك فوراً بالدولار عن كل شخص يشترك في البوت وينضم لقناتنا الرسمية.\n\n"
        "<b>📈 ميزتنا الشفافة:</b> كلما دعوت أشخاصاً أكثر، يرتفع مستواك التلقائي وتتضاعف قيمة مكافأتك لكل شخص! نظامنا محمي بالكامل لضمان احتساب وحفظ رصيدك بدقة.\n\n"
        "👇 اختر من الأزرار أدناه لبدء رحلتك ومتابعة أرباحك بكل ثقة:",    
        reply_markup=referrals_menu(),
    )
def wallet_message(summary: object) -> str:
    balance = format_amount(summary.balance, Currency.USD)
    reserved = format_amount(summary.reserved, Currency.USD)
    account = f"{summary.account_method}: {hcode(summary.account_value)}" if summary.account_method and summary.account_value else "لم يُضبط بعد"
    return (
        "💳<b>مـحـفـظـتـي ومـعـلـومـاتـي</b>💳\n\n\n"
        f"🔘الرصيد الحالي: <b>{balance}</b>\n\n"
        f"🔘الرصيد المحجوز للمراجعة: <b>{reserved}</b>\n\n"
        f"🔘عدد الإحالات: <b>{summary.referrals}</b>\n\n"
        f"🔘المستوى الحالي: <b>{summary.referral_level}</b>\n\n"
        f"🔘الحد الأدنى للسحب: <b>{format_amount(summary.withdrawal_minimum, Currency.USD)}</b>\n\n"
        f"🔘حساب السحب: {account}"
    )
from aiogram.exceptions import TelegramBadRequest

@router.callback_query(F.data == "menu:wallet")
async def wallet_callback(callback: CallbackQuery, app_context: ApplicationContext) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        return

    # نرد على ضغطة الزر فوراً قبل الاتصال بقاعدة البيانات
    await callback.answer("جارٍ تحديث المحفظة...")

    service, session = await service_session(app_context)
    try:
        summary = await service.wallet_summary(callback.from_user.id)

        try:
            await callback.message.edit_text(
                wallet_message(summary),
                reply_markup=wallet_menu(),
            )
        except TelegramBadRequest as error:
            # طبيعي إذا كانت الرسالة والأزرار كما هي؛ نتجاهل هذا الخطأ فقط
            if "message is not modified" not in str(error).lower():
                raise
    finally:
        await session.close()

@router.callback_query(F.data == "menu:referral")
async def referral_callback(callback: CallbackQuery, app_context: ApplicationContext, bot: Bot) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        return
    service, session = await service_session(app_context)
    try:
        summary = await service.wallet_summary(callback.from_user.id)
        bot_info = await bot.get_me()
        link = f"https://t.me/{bot_info.username}?start={summary.referral_code}"
        await callback.answer()
        await callback.message.edit_text(
            "🔗<b>رابـط الإحـالـة الـخـاص بـك</b>🔗\n\n\n"
            f"<code>{html.escape(link)}</code>\n\n\n"
            f"عدد الإحالات المؤكدة: <b>{summary.referrals}</b>\n\n"
            "تُحتسب الإحالة بعد اشتراك المستخدم الجديد بالبوت والقناة.",
            reply_markup=referrals_menu(),
        )
    finally:
        await session.close()


@router.callback_query(F.data == "menu:rewards")
async def rewards_callback(callback: CallbackQuery, app_context: ApplicationContext) -> None:
    if not callback.message or not is_private(callback):
        return
    service, session = await service_session(app_context)
    try:
        summary = await service.wallet_summary(callback.from_user.id)
        settings = app_context.settings
        referrals = int(summary.referrals)
        current_level = summary.referral_level
        next_level = referral_level_for_count(referrals + 1)
        next_reward = referral_reward_for_level(settings, next_level)
        level_names = {1: "الأول", 2: "الثاني", 3: "الثالث", 4: "الرابع"}
        tier_lines = (
            f"🥇المستوى الأول (0–20): {format_amount(settings.referral_level_1_reward, Currency.USD)} لكل إحالة\n\n"
            f"🥈المستوى الثاني (21–50): {format_amount(settings.referral_level_2_reward, Currency.USD)} لكل إحالة\n\n"
            f"🥉المستوى الثالث (51–100): {format_amount(settings.referral_level_3_reward, Currency.USD)} لكل إحالة\n\n"
            f"🏅المستوى الرابع (101+): {format_amount(settings.referral_level_4_reward, Currency.USD)} لكل إحالة"
        )
        text = (
            "🎁<b>الـمـسـتـوى و الـمـكـافـآت</b>🎁\n\n\n"
            f"▫️المستوى الحالي: <b>{level_names.get(current_level, 'الأول')}</b> ({current_level})\n\n"
            f"▫️عدد إحالاتك: <b>{referrals}</b>\n\n"
            f"▫️مكافأة الإحالة التالية (للمستوى {next_level}): <b>{format_amount(next_reward, Currency.USD)}</b>\n\n"
            f"▫️مكافأة التسجيل: <b>{format_amount(settings.referral_signup_bonus, Currency.USD)}</b>\n\n\n"
            f"{tier_lines}\n\n"
            "المكافآت تُحسب للإحالات بعد الإشتراك بالبوت والقناة فقط."
        )
        await callback.answer()
        await callback.message.edit_text(text, reply_markup=referrals_menu())
    finally:
        await session.close()


@router.callback_query(F.data == "withdrawal:account")
async def withdrawal_account_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        return
    await state.clear()
    await state.set_state(UserFlow.awaiting_withdrawal_account)
    await callback.answer()
    await callback.message.edit_text("اخـتـر طـريـقـة الـسـحـب:", reply_markup=withdrawal_method_menu())

async def present_crypto_request_review(
    *,
    bot: Bot,
    service,
    state: FSMContext,
    app_context: ApplicationContext,
    request,
    user_id: int,
    chat_id: int,
    edit_message: Message | None = None,
) -> None:
    if request.route_code in {RouteCode.USD_TO_USDT, RouteCode.USDT_TO_USD}:
        pricing_summary = (
            f"عمولة الخدمة: "
            f"<b>{display_percentage(request.fee_percentage_snapshot)}%</b> "
            "(تُخصم من مبلغ الاستلام).\n"
            f"صافي الاستلام بعد العمولة: "
            f"<b>{format_amount(request.to_amount or 0, request.to_currency)}</b>\n"
            "سعر الأساس: 1 دولار = 1 USDT"
        )
    else:
        rate_unit = (
            "دولار واحد"
            if request.route_code in {RouteCode.SYP_TO_USD, RouteCode.USD_TO_SYP}
            else "USDT واحد"
        )
        pricing_summary = (
            f"<b>عـمـولـة الـخـدمـة: </b>"
            f"<b>{display_percentage(request.fee_percentage_snapshot)}%</b>\n\n"
            f"<b>صافي الاستلام بعد العمولة: </b>"
            f"<b>{format_amount(request.to_amount or 0, request.to_currency)}</b>\n\n"
            f"<b>السعر المثبّت للطلب: </b>"
            f"<b>{format_rate(request.rate_snapshot)} ل.س لكل {rate_unit}</b>"
        )

    crypto_method_line = ""
    if request.crypto_method:
        crypto_method_line = (
            f"🔹<b>طريقة USDT:</b> "
            f"<b>{html.escape(CRYPTO_METHOD_LABELS[request.crypto_method])}</b>\n\n"
        )

    review_expiry_time = ensure_utc(request.expires_at).astimezone(
        ZoneInfo("Asia/Damascus")
    ).strftime("%H:%M")

    review_text = (
        f"📋<b>مراجعة الطلب رقم #{hcode(request.public_id)}</b>\n\n"
        f"📝<b>الـطـلـب:</b>\n"
        f"<b>{ROUTE_LABELS[request.route_code]}</b>\n\n"
        f"📤<b>مبلغ الدفع المطلوب:</b> "
        f"<b>{format_amount(request.from_amount or 0, request.from_currency)}</b>\n\n"
        f"📥<b>مبلغ الاستلام:</b> "
        f"<b>{format_amount(request.to_amount or 0, request.to_currency)}</b>\n\n"
        f"{crypto_method_line}"
        f"🪪<b>عنوان الاستلام:</b>\n"
        f"{hcode(request.payout_address or '')}\n\n"
        f"📊{pricing_summary}\n\n"
        f"<b>الحد الأدنى المعتمد:</b>\n"
        f"<b>{format_amount(request.minimum_usd_snapshot, Currency.USD)}</b> ؛ (أو ما يعادلها حسب سعر الصرف)\n"
        "ويُتحقق منه وفق سعر التحويل المثبّت لهذا الطلب.\n\n"
        f"⏱️<b>صلاحية عرض السعر:  </b>\n"
        f"<b>حتى الساعة: {review_expiry_time}</b> بتوقيت سوريا "
        f"⏳(مهلة {app_context.settings.review_ttl_minutes} دقائق).\n\n"
        "اختر «تأكيد» للانتقال إلى بيانات الدفع والباركود،\n"
        "أو «تعديل» لتغيير البيانات، أو «إلغاء الطلب»."
    )

    if edit_message is not None:
        review_message = await edit_message.edit_text(
            review_text,
            reply_markup=request_confirmation_menu(),
        )
    else:
        review_message = await bot.send_message(
            chat_id=chat_id,
            text=review_text,
            reply_markup=request_confirmation_menu(),
        )

    await service.set_user_review_message_id(
        request.public_id,
        user_id,
        review_message.message_id,
    )

    await state.set_state(UserFlow.awaiting_user_review)
    await state.update_data(
        request_id=request.public_id,
        route_code=request.route_code.value,
        prompt_chat_id=review_message.chat.id,
        prompt_message_id=review_message.message_id,
    )

    review_expiry_task = asyncio.create_task(
        expire_user_review_message(
            bot=bot,
            app_context=app_context,
            state=state,
            user_id=user_id,
            chat_id=review_message.chat.id,
            request_id=request.public_id,
            review_message_id=review_message.message_id,
            expires_at=request.expires_at,
        )
    )
    track_expiry_task(request.public_id, review_expiry_task)

@router.callback_query(F.data.startswith("withdrawal:method:"))
async def withdrawal_method_selected(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        return
    method = (callback.data or "").removeprefix("withdrawal:method:")
    await state.update_data(withdrawal_method=method)
    await state.set_state(UserFlow.awaiting_withdrawal_account)
    await callback.answer()
    await callback.message.edit_text(f"أرسـل حـسـاب الـسـحـب الـخـاص بـك لـطـريـقـة {html.escape(method)} فـي رسـالـة واحـدة.", reply_markup=cancel_menu())


@router.message(UserFlow.awaiting_withdrawal_account, F.text)
async def withdrawal_account_received(message: Message, state: FSMContext, app_context: ApplicationContext) -> None:
    if not message.from_user:
        return
    data = await state.get_data()
    method = data.get("withdrawal_method")
    if not isinstance(method, str):
        await message.answer("اخـتـر طـريـقـة الـسـحـب أولاً.", reply_markup=withdrawal_method_menu())
        return
    service, session = await service_session(app_context)
    try:
        await service.save_withdrawal_account(message.from_user.id, method, message.text or "")
        await state.clear()
        await message.answer("✅تـم حـفـظ حـسـاب الـسـحـب بـنـجـاح✅", reply_markup=referrals_menu())
    except DomainError as error:
        await message.answer(html.escape(str(error)), reply_markup=cancel_menu())
    finally:
        await session.close()


@router.callback_query(F.data == "menu:withdrawal")
async def withdrawal_start(callback: CallbackQuery, state: FSMContext, app_context: ApplicationContext) -> None:
    if not callback.message or not callback.from_user or not is_private(callback):
        return
    service, session = await service_session(app_context)
    try:
        summary = await service.wallet_summary(callback.from_user.id)
        if not summary.account_method:
            await callback.answer()
            await callback.message.edit_text("💳 أضـف حـسـاب الـسحـب أولاً:", reply_markup=withdrawal_method_menu())
            return
        await state.set_state(UserFlow.awaiting_withdrawal_amount)
        await callback.answer()
        await callback.message.edit_text(
            f"💵 أرسـل مـبـلـغ الـسـحـب بـالـدولار.\n الـحـد الأدنـى هـو <b>{format_amount(app_context.settings.withdrawal_minimum_usd, Currency.USD)}</b>.",
            reply_markup=cancel_menu(),
        )
    finally:
        await session.close()


@router.message(UserFlow.awaiting_withdrawal_amount, F.text)
async def withdrawal_amount_received(message: Message, state: FSMContext, app_context: ApplicationContext, bot: Bot) -> None:
    if not message.from_user:
        return
    service, session = await service_session(app_context)
    try:
        request = await service.create_withdrawal(message.from_user.id, message.text or "")
        await state.clear()
        text = (
            "✅ <b>تم تسجيل طلب السحب</b>\n\n"
            f"رقم الطلب: <b>#{hcode(request.public_id)}</b>\n"
            f"المبلغ: <b>{format_amount(request.amount, Currency.USD)}</b>\n"
            f"الحالة: قيد المراجعة لمدة {app_context.settings.withdrawal_review_hours} ساعة على الأقل."
        )
        await message.answer(text, reply_markup=main_menu(app_context.is_admin(message.from_user.id), app_context.settings.contact_url))
        for admin_id in app_context.settings.admin_ids:
            try:
                await bot.send_message(admin_id, f"طلب سحب جديد #{hcode(request.public_id)} من المستخدم {message.from_user.id} بمبلغ {format_amount(request.amount, Currency.USD)}")
            except TelegramAPIError:
                logger.info("حاول لاحقاً.")
    except DomainError as error:
        await message.answer(html.escape(str(error)), reply_markup=cancel_menu())
    finally:
        await session.close()


@router.callback_query(F.data == "admin:home")
async def admin_home(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    
    if not await require_admin(callback, app_context) or not callback.message:
        return
        
    await state.clear()
        
    service, session = await service_session(app_context)
    try:
        stats = await service.dashboard_stats()
        monitoring = await service.monitoring_enabled()
        await callback.answer()
        await callback.message.edit_text(
            "<b>لوحة التحكم الشاملة</b>\n"
            f"• طلبات قيد المراجعة: <b>{stats.pending_review}</b>\n"
            f"• طلبات غير مكتملة: <b>{stats.drafts_in_progress}</b>\n"
            f"• عمليات مكتملة اليوم: <b>{stats.completed_today}</b>\n"
            f"• إجمالي العمليات المكتملة: <b>{stats.completed_total}</b>\n"
            f"• إجمالي المستخدمين: <b>{stats.total_users}</b>\n"
            f"• المستخدمون النشطون اليوم: <b>{stats.active_today}</b>\n"
            f"• إجمالي الإحالات: <b>{stats.total_referrals}</b>\n"
            f"• السحوبات المدفوعة: <b>{format_amount(stats.total_withdrawal_paid, Currency.USD)}</b>",
            reply_markup=admin_home_menu(monitoring=monitoring),
        )
    finally:
        await session.close()


@router.callback_query(F.data == "admin:crypto-methods")
async def admin_crypto_methods(
    callback: CallbackQuery,
    app_context: ApplicationContext,
) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return

    await callback.answer()
    await callback.message.edit_text(
        "اختر طريقة USDT لضبط عنوان/حساب الدفع وصورة QR الخاصة بها.",
        reply_markup=crypto_methods_menu(),
    )


@router.callback_query(F.data.startswith("admin:crypto-method:"))
async def admin_crypto_method_detail(
    callback: CallbackQuery,
    app_context: ApplicationContext,
) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return

    method = (callback.data or "").removeprefix("admin:crypto-method:")
    if method not in CRYPTO_METHOD_LABELS:
        await callback.answer("طريقة USDT غير معروفة.", show_alert=True)
        return

    service, session = await service_session(app_context)
    try:
        address, barcode = await service.crypto_method_values(method)
    finally:
        await session.close()

    address_status = "مضبوط" if address else "غير مضبوط"
    barcode_status = "مضبوط" if barcode else "غير مضبوط"

    await callback.answer()
    await callback.message.edit_text(
        f"<b>{CRYPTO_METHOD_LABELS[method]}</b>\n"
        f"العنوان أو الحساب: <b>{address_status}</b>\n"
        f"صورة QR: <b>{barcode_status}</b>\n\n"
        "أدخلي بيانات هذه الطريقة من الزرين أدناه.",
        reply_markup=crypto_method_detail_menu(method),
    )

@router.callback_query(F.data.startswith("admin:crypto-address:"))
async def admin_crypto_address_prompt(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return

    method = (callback.data or "").removeprefix("admin:crypto-address:")
    if method not in CRYPTO_METHOD_LABELS:
        await callback.answer("طريقة USDT غير معروفة.", show_alert=True)
        return

    await state.set_state(AdminFlow.awaiting_route_address)
    await state.update_data(route_code=None, crypto_method=method)
    await callback.answer()
    await callback.message.edit_text(
        f"أرسلي عنوان المحفظة أو البريد/رقم الحساب لطريقة "
        f"<b>{CRYPTO_METHOD_LABELS[method]}</b>.",
        reply_markup=crypto_method_detail_menu(method),
    )


@router.callback_query(F.data.startswith("admin:crypto-photo:"))
async def admin_crypto_photo_prompt(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return

    method = (callback.data or "").removeprefix("admin:crypto-photo:")
    if method not in CRYPTO_METHOD_LABELS:
        await callback.answer("طريقة USDT غير معروفة.", show_alert=True)
        return

    await state.set_state(AdminFlow.awaiting_route_photo)
    await state.update_data(route_code=None, crypto_method=method)
    await callback.answer()
    await callback.message.edit_text(
        f"أرسلي صورة QR الخاصة بطريقة <b>{CRYPTO_METHOD_LABELS[method]}</b>.",
        reply_markup=crypto_method_detail_menu(method),
    )

@router.callback_query(F.data == "admin:referrals")
async def admin_referrals(callback: CallbackQuery, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    service, session = await service_session(app_context)
    try:
        rows = await service.referral_admin_leaderboard(50)
        if not rows:
            text = "لا توجد إحالات مؤكدة حتى الآن."
        else:
            text = "<b>إدارة الإحالات</b>\n\n" + "\n".join(
                f"{index}. {html.escape(row.display_name)} — <code>{row.user_id}</code> — "
                f"<b>{row.referrals}</b> إحالة"
                for index, row in enumerate(rows, 1)
            )
        await callback.answer()
        await callback.message.edit_text(text, reply_markup=admin_referrals_menu())
    finally:
        await session.close()


@router.callback_query(F.data == "admin:withdrawals")
async def admin_withdrawals(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    await state.clear()
    service, session = await service_session(app_context)
    try:
        requests = await service.pending_withdrawals()
        if not requests:
            text = "لا توجد طلبات سحب قيد المراجعة."
            markup = admin_home_menu()
        else:
            text = "<b>طلبات السحب قيد المراجعة</b>\n\n" + "\n\n".join(
                f"#{hcode(request.public_id)} — المستخدم <code>{request.user_id}</code> — "
                f"{format_amount(request.amount, Currency.USD)} — {html.escape(request.method_snapshot)}"
                for request in requests
            )
            markup = admin_withdrawal_list_menu([request.public_id for request in requests])
        await callback.answer()
        await callback.message.edit_text(text, reply_markup=markup)
    finally:
        await session.close()


async def _show_admin_withdrawal_detail(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
    public_id: str,
) -> None:
    service, session = await service_session(app_context)
    try:
        requests = await service.pending_withdrawals()
        request = next((item for item in requests if item.public_id == public_id), None)
        if request is None:
            await callback.answer("طلب السحب غير متاح للمراجعة.", show_alert=True)
            return

        await callback.message.edit_text(
            "<b>تفاصيل طلب السحب</b>\n"
            f"رقم الطلب: #{hcode(request.public_id)}\n"
            f"المستخدم: <code>{request.user_id}</code>\n"
            f"المبلغ: <b>{format_amount(request.amount, Currency.USD)}</b>\n"
            f"الطريقة: {html.escape(request.method_snapshot)}\n"
            f"وجهة السحب: <code>{html.escape(request.account_snapshot)}</code>",
            reply_markup=admin_withdrawal_menu(public_id),
        )
        await state.set_state(AdminFlow.awaiting_withdrawal_decision)
        await state.update_data(
            withdrawal_review_id=public_id,
            withdrawal_review_message_id=callback.message.message_id,
        )
        await callback.answer()
    finally:
        await session.close()


@router.callback_query(F.data.startswith("admin:withdrawal:view:"))
async def admin_withdrawal_detail(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return

    public_id = (callback.data or "").removeprefix("admin:withdrawal:view:")
    await _show_admin_withdrawal_detail(callback, state, app_context, public_id)


@router.callback_query(F.data.startswith("admin:withdrawal:"))
async def admin_resolve_withdrawal(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
    bot: Bot,
) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await _admin_resolve_withdrawal_locked(callback, state, app_context, bot)
        return
    lock = _WITHDRAWAL_LOCKS.setdefault(parts[3], asyncio.Lock())
    async with lock:
        await _admin_resolve_withdrawal_locked(callback, state, app_context, bot)


async def _admin_resolve_withdrawal_locked(
    callback: CallbackQuery,
    state: FSMContext,
    app_context: ApplicationContext,
    bot: Bot,
) -> None:
    if not await require_admin(callback, app_context) or not callback.message or not callback.from_user:
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 4 or parts[2] not in {"approve", "reject"}:
        await callback.answer("إجراء غير صالح.", show_alert=True)
        return
    admin_data = await state.get_data()
    if (
        await state.get_state() != AdminFlow.awaiting_withdrawal_decision.state
        or admin_data.get("withdrawal_review_id") != parts[3]
        or admin_data.get("withdrawal_review_message_id") != callback.message.message_id
        or "تفاصيل طلب السحب" not in (callback.message.text or "")
    ):
        await _show_admin_withdrawal_detail(
            callback,
            state,
            app_context,
            parts[3],
        )
        return
    approved = parts[2] == "approve"
    service, session = await service_session(app_context)
    try:
        request = await service.resolve_withdrawal(parts[3], callback.from_user.id, approved)
        await state.clear()
        await callback.answer("تم تنفيذ القرار.")
        await callback.message.edit_text(
            f"تم {'اعتماد ودفع' if approved else 'رفض'} طلب السحب #{hcode(request.public_id)}.",
            reply_markup=admin_home_menu(),
        )
        try:
            await bot.send_message(
                request.user_id,
                f"تم {'اعتماد ودفع' if approved else 'رفض'} طلب السحب رقم #{hcode(request.public_id)}.",
                reply_markup=main_menu(False, app_context.settings.contact_url),
            )
        except TelegramAPIError:
            logger.info("تعذر إرسال نتيجة طلب السحب للمستخدم %s.", request.user_id)
    except DomainError as error:
        await callback.answer(str(error), show_alert=True)
    finally:
        await session.close()


@router.callback_query(F.data == "admin:rates")
async def admin_rates(callback: CallbackQuery, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    service, session = await service_session(app_context)
    try:
        await callback.answer()
        await callback.message.edit_text(rates_text(await service.current_rates()), reply_markup=rates_menu())
    finally:
        await session.close()


@router.callback_query(F.data.startswith("admin:rate:"))
async def admin_rate_prompt(callback: CallbackQuery, state: FSMContext, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    key = (callback.data or "").removeprefix("admin:rate:")

    if key == "minimum_usd":
        await callback.answer(
            "الحد الأدنى موحّد ويُدار من MINIMUM_USD في ملف البيئة؛ أعد تشغيل البوت بعد تغييره.",
            show_alert=True,
        )
        return

    if key not in RATE_LABELS:
        await callback.answer("إعداد غير صالح.", show_alert=True)
        return
    await state.set_state(AdminFlow.awaiting_rate)
    await state.update_data(setting_key=key)
    await callback.answer()
    await callback.message.edit_text(
        f"أدخل القيمة الجديدة لـ <b>{RATE_LABELS[key]}</b> كرقم فقط.", reply_markup=admin_edit_menu()
    )


@router.message(AdminFlow.awaiting_rate, F.text)
async def save_rate(message: Message, state: FSMContext, app_context: ApplicationContext) -> None:
    if not message.from_user or not app_context.is_admin(message.from_user.id):
        return
    key = (await state.get_data()).get("setting_key")
    if not isinstance(key, str):
        await state.clear()
        return
    if key == "minimum_usd":
        await state.clear()
        await message.answer(
            "الحد الأدنى موحّد ويُدار من MINIMUM_USD في ملف البيئة؛ أعد تشغيل البوت بعد تغييره.",
            reply_markup=admin_home_menu(),
        )
        return
    service, session = await service_session(app_context)
    try:
        value = await service.update_rate(key, message.text or "", message.from_user.id)
        await state.clear()
        await message.answer(f"تم تحديث <b>{RATE_LABELS[key]}</b> إلى <b>{value}</b>.", reply_markup=admin_home_menu())
    except DomainError as error:
        await message.answer(html.escape(str(error)), reply_markup=admin_edit_menu())
    finally:
        await session.close()

@router.callback_query(F.data == "admin:routes")
async def admin_routes(callback: CallbackQuery, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    service, session = await service_session(app_context)
    try:
        routes = await service.all_routes()
        await callback.answer()
        await callback.message.edit_text("<b>إعداد المسارات والباركودات</b>", reply_markup=route_settings_menu(routes))
    finally:
        await session.close()


@router.callback_query(F.data == "admin:payment-routes")
async def admin_payment_routes(callback: CallbackQuery, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    service, session = await service_session(app_context)
    try:
        routes = await service.all_routes()
        await callback.answer()
        await callback.message.edit_text("اختر المسار لتعديل بيانات الدفع والباركود.", reply_markup=route_settings_menu(routes))
    finally:
        await session.close()


@router.callback_query(F.data.startswith("admin:route:"))
async def admin_route_detail(callback: CallbackQuery, state: FSMContext, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    await state.clear()
        
    try:
        route_code = RouteCode((callback.data or "").removeprefix("admin:route:"))
    except ValueError:
        await callback.answer("مسار غير صالح.", show_alert=True)
        return
    service, session = await service_session(app_context)
    try:
        route = await service.get_route(route_code)
        route_status = "مفعّل" if route.is_enabled else "متوقف"
        address_status = "مضبوط" if route.payment_address else "غير مضبوط"
        barcode_status = "مرفوع" if route.barcode_file_id else "غير مرفوع"

        text = (
            f"<b>{ROUTE_LABELS[route.code]}</b>\n"
            f"الحالة: <b>{route_status}</b>\n"
            f"عنوان الدفع أو الحساب: <b>{address_status}</b>\n"
            f"صورة الباركود: <b>{barcode_status}</b>"
        )

        await callback.answer()
        await callback.message.edit_text(text, reply_markup=route_detail_menu(route))
    finally:
        await session.close()


@router.callback_query(F.data.startswith("admin:addr:"))
async def admin_address_prompt(callback: CallbackQuery, state: FSMContext, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    try:
        route_code = RouteCode((callback.data or "").removeprefix("admin:addr:"))
    except ValueError:
        await callback.answer("مسار غير صالح.", show_alert=True)
        return
    await state.set_state(AdminFlow.awaiting_route_address)
    await state.update_data(route_code=route_code.value)
    await callback.answer()
    await callback.message.edit_text(
        "أرسل عنوان المحفظة أو كود الدفع الذي سيظهر تحت باركود هذا المسار.", reply_markup=admin_edit_menu(route_code)
    )


@router.message(AdminFlow.awaiting_route_address, F.text)
async def save_route_address(
    message: Message,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not message.from_user or not app_context.is_admin(message.from_user.id):
        return

    data = await state.get_data()
    crypto_method = data.get("crypto_method")

    service, session = await service_session(app_context)
    try:
        if isinstance(crypto_method, str):
            value = await service.update_crypto_method_address(
                crypto_method,
                message.text or "",
                message.from_user.id,
            )
            await state.clear()
            await message.answer(
                f"تم حفظ العنوان/الحساب لطريقة "
                f"<b>{CRYPTO_METHOD_LABELS[crypto_method]}</b>.",
                reply_markup=crypto_method_detail_menu(crypto_method),
            )
            return

        try:
            route_code = RouteCode(str(data.get("route_code")))
        except ValueError:
            await state.clear()
            await message.answer("تعذر تحديد المسار. ابدئي إعداد المسار من جديد.")
            return

        await service.update_route_address(
            route_code,
            message.text or "",
            message.from_user.id,
        )
        await state.clear()
        await message.answer(
            "تم تحديث عنوان / كود الدفع للمسار.",
            reply_markup=admin_home_menu(),
        )
    except DomainError as error:
        await message.answer(
            html.escape(str(error)),
            reply_markup=(
                crypto_method_detail_menu(crypto_method)
                if isinstance(crypto_method, str)
                else admin_edit_menu(route_code)
            ),
        )
    finally:
        await session.close()

@router.callback_query(F.data.startswith("admin:photo:"))
async def admin_photo_prompt(callback: CallbackQuery, state: FSMContext, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    try:
        route_code = RouteCode((callback.data or "").removeprefix("admin:photo:"))
    except ValueError:
        await callback.answer("مسار غير صالح.", show_alert=True)
        return
    await state.set_state(AdminFlow.awaiting_route_photo)
    await state.update_data(route_code=route_code.value)
    await callback.answer()
    await callback.message.edit_text("أرسل الآن <b>صورة</b> باركود هذا المسار.", reply_markup=admin_edit_menu(route_code))


@router.message(AdminFlow.awaiting_route_photo, F.photo)
async def save_route_photo(
    message: Message,
    state: FSMContext,
    app_context: ApplicationContext,
) -> None:
    if not message.from_user or not app_context.is_admin(message.from_user.id):
        return

    data = await state.get_data()
    crypto_method = data.get("crypto_method")
    file_id = message.photo[-1].file_id

    service, session = await service_session(app_context)
    try:
        if isinstance(crypto_method, str):
            await service.update_crypto_method_barcode(
                crypto_method,
                file_id,
                message.from_user.id,
            )
            await state.clear()
            await message.answer(
                f"تم حفظ صورة QR لطريقة "
                f"<b>{CRYPTO_METHOD_LABELS[crypto_method]}</b>.",
                reply_markup=crypto_method_detail_menu(crypto_method),
            )
            return

        try:
            route_code = RouteCode(str(data.get("route_code")))
        except ValueError:
            await state.clear()
            await message.answer("تعذر تحديد المسار. ابدئي إعداد المسار من جديد.")
            return

        await service.update_route_barcode(
            route_code,
            file_id,
            message.from_user.id,
        )
        await state.clear()
        await message.answer(
            "تم حفظ صورة الباركود للمسار.",
            reply_markup=admin_home_menu(),
        )
    except DomainError as error:
        await message.answer(
            html.escape(str(error)),
            reply_markup=(
                crypto_method_detail_menu(crypto_method)
                if isinstance(crypto_method, str)
                else admin_edit_menu(route_code)
            ),
        )
    finally:
        await session.close()


@router.message(AdminFlow.awaiting_route_photo)
async def invalid_route_photo(message: Message) -> None:
    await message.answer("أرسل الباركود كصورة، وليس كملف أو نص.", reply_markup=cancel_menu())


@router.callback_query(F.data.startswith("admin:toggle:"))
async def toggle_route(callback: CallbackQuery, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message or not callback.from_user:
        return
    try:
        _, _, code_value, enabled_value = (callback.data or "").split(":")
        route_code = RouteCode(code_value)
        if enabled_value not in {"0", "1"}:
            raise ValueError
        enabled = enabled_value == "1"
    except ValueError:
        await callback.answer("بيانات المسار غير صالحة.", show_alert=True)
        return
    service, session = await service_session(app_context)
    try:
        await service.set_route_enabled(route_code, enabled, callback.from_user.id)
        await callback.answer("تم تحديث حالة المسار.")
        await callback.message.edit_text("تم تحديث حالة المسار بنجاح.", reply_markup=admin_home_menu())
    finally:
        await session.close()


@router.callback_query(F.data == "admin:pending")
async def admin_pending(
    callback: CallbackQuery,
    app_context: ApplicationContext,
    bot: Bot,
) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    service, session = await service_session(app_context)
    try:
        requests = await service.pending_requests()
        await callback.answer()
        if not requests:
            try:
                await callback.message.delete()
            except TelegramAPIError:
                logger.warning("حاول لاحقاً.")

            await bot.send_message(
                chat_id=callback.message.chat.id,
                text="لا توجد طلبات بانتظار الدفع أو التحقق حالياً.",
                reply_markup=admin_home_menu(),
            )
        else:
            try:
                await callback.message.delete()
            except TelegramAPIError:
                logger.warning("حاول لاحقاً..")

            await bot.send_message(
                chat_id=callback.message.chat.id,
                text=(
                    f"<b>الطلبات بانتظار الدفع أو التحقق ({len(requests)})</b>\n"
                    "أزرار الإتمام متاحة فقط بعد إبلاغ المستخدم عن الدفع."
                ),
                reply_markup=admin_request_list_menu(
                    [request.public_id for request in requests]
                ),
            )

    finally:
        await session.close()


@router.callback_query(F.data.startswith("admin:view:"))
async def admin_view_request(
    callback: CallbackQuery,
    app_context: ApplicationContext,
    bot: Bot,
) -> None:

    if not await require_admin(callback, app_context) or not callback.message:
        return
    public_id = (callback.data or "").removeprefix("admin:view:")
    service, session = await service_session(app_context)
    try:
        request = await service.get_request_for_admin(public_id)
        await callback.answer()
        reply_markup = (
            admin_payment_waiting_menu()
            if request.status is RequestStatus.PENDING_PAYMENT
            else review_menu(public_id)
        )
        try:
            await callback.message.delete()
        except TelegramAPIError:
            logger.warning("حاول لاحقاً %s.", public_id)

        await bot.send_message(
            chat_id=callback.message.chat.id,
            text=admin_request_summary(request),
            reply_markup=reply_markup,
        )

    except DomainError as error:
        await callback.answer(str(error), show_alert=True)
    finally:
        await session.close()


@router.callback_query(F.data == "menu:contact")
async def contact_callback(callback: CallbackQuery, app_context: ApplicationContext) -> None:
    if not callback.message:
        return
    await callback.answer()
    if app_context.settings.contact_url:
        await callback.message.answer(f"للتواصل معنا: {html.escape(app_context.settings.contact_url)}")
    else:
        await callback.message.answer("لم تُضبط وسيلة التواصل بعد. يُرجى التواصل مع إدارة البوت.")


@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    service, session = await service_session(app_context)
    try:
        stats = await service.dashboard_stats()
        await callback.answer()
        await callback.message.edit_text(
            "<b>إحصاءات البوت</b>\n"
            f"إجمالي المستخدمين: <b>{stats.total_users}</b>\n"
            f"النشطون اليوم: <b>{stats.active_today}</b>\n"
            f"الطلبات قيد المراجعة: <b>{stats.pending_review}</b>\n"
            f"الطلبات غير المكتملة: <b>{stats.drafts_in_progress}</b>\n"
            f"العمليات المكتملة اليوم: <b>{stats.completed_today}</b>\n"
            f"إجمالي العمليات المكتملة: <b>{stats.completed_total}</b>", reply_markup=admin_home_menu())
    finally:
        await session.close()


@router.callback_query(F.data == "admin:ban")
async def admin_ban_menu(callback: CallbackQuery, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    await callback.answer()
    await callback.message.edit_text("إدارة صلاحية المستخدمين:", reply_markup=user_management_menu())


@router.callback_query(F.data.startswith("admin:ban:"))
async def admin_ban_prompt(callback: CallbackQuery, state: FSMContext, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    action = (callback.data or "").removeprefix("admin:ban:")
    if action not in {"set", "unset"}:
        await callback.answer("حاول لاحقاً.", show_alert=True)
        return
    await state.set_state(AdminFlow.awaiting_ban_id)
    await state.update_data(ban_action=action)
    await callback.answer()
    await callback.message.edit_text("أرسل معرّف تيليغرام الرقمي للمستخدم.", reply_markup=cancel_menu())


@router.message(AdminFlow.awaiting_ban_id, F.text)
async def save_ban_status(message: Message, state: FSMContext, app_context: ApplicationContext) -> None:
    if not message.from_user or not app_context.is_admin(message.from_user.id):
        return
    try:
        user_id = int((message.text or "").strip())
        if user_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("أرسل معرّف تيليغرام رقميّاً صحيحاً.", reply_markup=cancel_menu())
        return
    service, session = await service_session(app_context)
    try:
        action = (await state.get_data()).get("ban_action")
        await service.set_user_banned(user_id, action == "set", message.from_user.id)
        await state.clear()
        await message.answer("تم حظر المستخدم." if action == "set" else "تم رفع الحظر عن المستخدم.", reply_markup=admin_home_menu())
    except DomainError as error:
        await message.answer(html.escape(str(error)), reply_markup=admin_home_menu())
        await state.clear()
    finally:
        await session.close()


@router.callback_query(F.data == "admin:monitor-toggle")
async def toggle_monitoring(callback: CallbackQuery, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message or not callback.from_user:
        return
    service, session = await service_session(app_context)
    try:
        enabled = not await service.monitoring_enabled()
        await service.set_monitoring(enabled, callback.from_user.id)
        app_context.cache_monitoring_enabled(enabled)
        await callback.answer("تم تحديث وضع المتابعة.")
        await callback.message.edit_text("تم تفعيل متابعة رسائل المستخدمين." if enabled else "تم إيقاف متابعة رسائل المستخدمين.",
                                      reply_markup=admin_home_menu(monitoring=enabled))
    finally:
        await session.close()


@router.callback_query(F.data == "admin:broadcast")
async def broadcast_choices(callback: CallbackQuery, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    await callback.answer()
    await callback.message.edit_text("اختر نطاق الإرسال:", reply_markup=broadcast_menu())


@router.callback_query(F.data == "admin:broadcast:all")
async def broadcast_all_prompt(callback: CallbackQuery, state: FSMContext, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    await state.set_state(AdminFlow.awaiting_broadcast_content)
    await state.update_data(broadcast_target="all")
    await callback.answer()
    await callback.message.edit_text("أرسل الرسالة أو الصورة أو الوسيط الذي تريد بثه. لن يُرسل قبل تأكيدك.", reply_markup=cancel_menu())


@router.callback_query(F.data == "admin:broadcast:one")
async def broadcast_one_prompt(callback: CallbackQuery, state: FSMContext, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    await state.set_state(AdminFlow.awaiting_direct_user_id)
    await callback.answer()
    await callback.message.edit_text("أرسل معرّف المستخدم الرقمي الذي تريد مراسلته.", reply_markup=cancel_menu())


@router.message(AdminFlow.awaiting_direct_user_id, F.text)
async def receive_direct_user_id(message: Message, state: FSMContext, app_context: ApplicationContext) -> None:
    if not message.from_user or not app_context.is_admin(message.from_user.id):
        return
    try:
        target = int((message.text or "").strip())
        if target <= 0: raise ValueError
    except ValueError:
        await message.answer("أرسل معرّفاً رقمياً صحيحاً.", reply_markup=cancel_menu())
        return
    await state.set_state(AdminFlow.awaiting_broadcast_content)
    await state.update_data(broadcast_target=str(target))
    await message.answer("أرسل الآن الرسالة أو الوسيط المطلوب إرساله إلى هذا المستخدم.", reply_markup=cancel_menu())


@router.message(AdminFlow.awaiting_broadcast_content)
async def receive_broadcast_content(message: Message, state: FSMContext, app_context: ApplicationContext) -> None:
    if not message.from_user or not app_context.is_admin(message.from_user.id):
        return
    await state.update_data(broadcast_chat_id=message.chat.id, broadcast_message_id=message.message_id)
    await message.answer("تم حفظ الرسالة. هل تؤكد الإرسال؟", reply_markup=confirm_broadcast_menu())


@router.callback_query(F.data == "admin:broadcast:confirm")
async def confirm_broadcast(callback: CallbackQuery, state: FSMContext, app_context: ApplicationContext, bot: Bot) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    if not callback.from_user:
        return

    async with broadcast_lock(callback.from_user.id):
        data = await state.get_data()
        source_chat = data.get("broadcast_chat_id")
        source_message = data.get("broadcast_message_id")
        target = data.get("broadcast_target")
        if not source_chat or not source_message or not target:
            await callback.answer("انتهت صلاحية الرسالة. ابدأ الإرسال من جديد.", show_alert=True)
            await state.clear()
            return

        # المطالبة بالحالة قبل أي إرسال تمنع callback ثانياً من إعادة نفس البث.
        await state.clear()
        service, session = await service_session(app_context)
        try:
            recipients = ([user_id for user_id in await service.user_chat_ids()
                           if user_id not in app_context.settings.admin_ids]
                          if target == "all" else [int(target)])
        finally:
            await session.close()

        sent = failed = 0
        for recipient in recipients:
            try:
                await copy_message_with_retry(bot, recipient, int(source_chat), int(source_message))
                sent += 1
            except TelegramAPIError:
                failed += 1
            await asyncio.sleep(0.05)

        await callback.answer("اكتمل الإرسال.")
        try:
            await callback.message.edit_text(
                f"اكتمل الإرسال. وصل إلى {sent} مستخدمين، وتعذر الإرسال إلى {failed}.",
                reply_markup=admin_home_menu(),
            )
        except TelegramAPIError:
            logger.info("تعذر تحديث رسالة نتيجة البث للمدير.", exc_info=True)


@router.callback_query(F.data == "admin:broadcast:cancel")
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context):
        return
    await state.clear()
    await callback.answer("أُلغي الإرسال.")
    if callback.message:
        await callback.message.edit_text("تم إلغاء الإرسال.", reply_markup=admin_home_menu())


@router.callback_query(F.data.startswith("admin:reply:"))
async def monitor_reply_prompt(callback: CallbackQuery, state: FSMContext, app_context: ApplicationContext) -> None:
    if not await require_admin(callback, app_context) or not callback.message:
        return
    try:
        user_id = int((callback.data or "").removeprefix("admin:reply:"))
    except ValueError:
        await callback.answer("معرّف المستخدم غير صالح.", show_alert=True)
        return
    current_state = await state.get_state()
    current_data = await state.get_data()
    current_target = current_data.get("reply_user_id")
    if current_state == AdminFlow.awaiting_monitor_reply.state and current_target != user_id:
        await callback.answer(
            "لديك رد جارٍ لمستخدم آخر. أرسله أولاً أو اضغط «إلغاء العملية».",
            show_alert=True,
        )
        return

    recipient_name = "المستخدم"
    source = callback.message.text or callback.message.caption or ""
    for line in source.splitlines():
        if line.strip().startswith("الاسم:"):
            recipient_name = re.sub(r"<[^>]*>", "", line.split(":", 1)[1]).strip()
            recipient_name = html.unescape(recipient_name) or "المستخدم"
            break
    await state.set_state(AdminFlow.awaiting_monitor_reply)
    await state.update_data(reply_user_id=user_id)
    await callback.answer()
    await callback.message.answer(
        f"أرسل الآن الرد إلى <b>{html.escape(recipient_name)}</b> "
        f"(المعرّف: <code>{user_id}</code>).",
        reply_markup=cancel_menu(),
    )


@router.message(AdminFlow.awaiting_monitor_reply)
async def send_monitor_reply(message: Message, state: FSMContext, app_context: ApplicationContext, bot: Bot) -> None:
    if not message.from_user or not app_context.is_admin(message.from_user.id):
        return
    user_id = (await state.get_data()).get("reply_user_id")
    if not user_id:
        await state.clear()
        return
    try:
        await bot.copy_message(int(user_id), message.chat.id, message.message_id)
        await message.answer("تم إرسال ردك إلى المستخدم.", reply_markup=admin_home_menu())
    except TelegramAPIError:
        await message.answer("تعذر إرسال الرسالة إلى المستخدم.", reply_markup=admin_home_menu())
    await state.clear()


@router.callback_query(F.data.startswith("admin:ok:"))
async def approve_request(callback: CallbackQuery, app_context: ApplicationContext, bot: Bot) -> None:
    await resolve_request(callback, app_context, bot, approved=True)


@router.callback_query(F.data.startswith("admin:no:"))
async def reject_request(callback: CallbackQuery, app_context: ApplicationContext, bot: Bot) -> None:
    await resolve_request(callback, app_context, bot, approved=False)


async def resolve_request(callback: CallbackQuery, app_context: ApplicationContext, bot: Bot, approved: bool) -> None:
    if not await require_admin(callback, app_context) or not callback.message or not callback.from_user:
        return
    prefix = "admin:ok:" if approved else "admin:no:"
    public_id = (callback.data or "").removeprefix(prefix)
    service, session = await service_session(app_context)
    try:
        resolution_lock = _REQUEST_RESOLUTION_LOCKS.setdefault(public_id, asyncio.Lock())
        async with resolution_lock:
            request = await service.resolve_request(public_id, callback.from_user.id, approved)
        if approved:
            customer_text = (
                "✅ <b>تمت الموافقة والتحويل</b>\n"
                f"طلبك #{hcode(request.public_id)} اكتمل.\n"
                f"المبلغ: <b>{format_amount(request.to_amount or 0, request.to_currency)}</b>"
            )
        else:
            customer_text = (
                "❌ <b>تم رفض الطلب</b>\n"
                f"طلبك #{hcode(request.public_id)} لم يُعتمد. يرجى التأكد من التحويل ورقم العملية ثم التواصل مع الإدارة."
            )
        try:
            await bot.send_message(request.chat_id, customer_text)
        except TelegramAPIError:
            logger.warning("Unable to notify customer for %s", request.public_id)
        await callback.answer("تمت معالجة الطلب.")
        try:
            await callback.message.delete()
        except TelegramAPIError:
            logger.warning("حاول لاحقاً%s.", public_id)

        await bot.send_message(
            chat_id=callback.message.chat.id,
            text="تم تحديث حالة الطلب وإرسال الإشعار للعميل.",
            reply_markup=admin_home_menu(),
        )

    except DomainError as error:
        await callback.answer(str(error), show_alert=True)
    finally:
        await session.close()


async def notify_admins(
    bot: Bot,
    app_context: ApplicationContext,
    request: ExchangeRequest,
) -> None:
    async def notify_one(admin_id: int) -> None:
        await bot.send_message(
            admin_id,
            admin_request_summary(request),
            reply_markup=review_menu(request.public_id),
        )

        if not request.payment_proof_file_id:
            return

        caption = f"إثبات الدفع للطلب #{request.public_id}"

        if request.payment_proof_type == "photo":
            await bot.send_photo(
                admin_id,
                photo=request.payment_proof_file_id,
                caption=caption,
            )
        elif request.payment_proof_type == "pdf":
            await bot.send_document(
                admin_id,
                document=request.payment_proof_file_id,
                caption=caption,
            )

    outcomes = await asyncio.gather(
        *(notify_one(admin_id) for admin_id in app_context.settings.admin_ids),
        return_exceptions=True,
    )

    for admin_id, outcome in zip(
        app_context.settings.admin_ids,
        outcomes,
        strict=True,
    ):
        if isinstance(outcome, Exception):
            logger.warning(
                "تعذر إرسال إشعار أو إثبات الدفع للأدمن %s: %s",
                admin_id,
                outcome,
            )

@router.error()
async def on_error(event: ErrorEvent) -> bool:
    if isinstance(event.exception, TelegramBadRequest):
        error_text = str(event.exception).lower()

        if (
            "message is not modified" in error_text
            or "query is too old" in error_text
            or "query id is invalid" in error_text
        ):
            return True

    logger.exception("Unhandled bot update error", exc_info=event.exception)
    message = event.update.message
    if message is None and event.update.callback_query:
        message = event.update.callback_query.message
    if message and message.chat.type == ChatType.PRIVATE:
        try:
            await message.answer("حاول لاحقاً.")
        except TelegramAPIError:
            pass
    return True

@router.message(F.chat.type == ChatType.PRIVATE)
async def unknown_message(message: Message, app_context: ApplicationContext) -> None:
    if message.from_user and app_context.is_admin(message.from_user.id):
        await message.answer("استخدم /start أو /admin.")
