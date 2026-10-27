"""حماية مركزية للتحديثات الخاصة وتسجيل المستخدمين ومتابعة الرسائل."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable
import weakref

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message

from app.context import ApplicationContext
from app.keyboards import monitoring_reply_menu, subscription_menu

logger = logging.getLogger(__name__)
MAX_MESSAGE_LENGTH = 2000
BURST_WINDOW_SECONDS = 8.0
BURST_MAX_MESSAGES = 5
MUTE_SECONDS = 12.0
RATE_STATE_CLEANUP_SECONDS = 60.0
MEMBERSHIP_CACHE_TTL_SECONDS = 30.0
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _split_by_utf16_units(value: str, max_units: int) -> list[str]:
    if max_units <= 0:
        return [value] if value else []
    chunks: list[str] = []
    current: list[str] = []
    current_units = 0
    for character in value:
        character_units = 2 if ord(character) > 0xFFFF else 1
        if current and current_units + character_units > max_units:
            chunks.append("".join(current))
            current = []
            current_units = 0
        current.append(character)
        current_units += character_units
    if current or not chunks:
        chunks.append("".join(current))
    return chunks


class ActivityGuard(BaseMiddleware):
    """يمنع الإغراق، يرفض النصوص الطويلة، ويسجل نشاط المستخدمين."""

    def __init__(self) -> None:
        self._events: dict[int, deque[float]] = defaultdict(deque)
        self._muted_until: dict[int, float] = {}
        self._last_warning: dict[int, float] = {}
        self._membership_cache: dict[int, float] = {}
        self._membership_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._last_cleanup = 0.0

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        app_context: ApplicationContext | None = data.get("app_context")
        bot: Bot | None = data.get("bot")
        user = event.from_user
        message = event if isinstance(event, Message) else None
        if not app_context or not user:
            return await handler(event, data)
        if message and message.chat.type != ChatType.PRIVATE:
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.message and event.message.chat.type != ChatType.PRIVATE:
            return await handler(event, data)
        is_admin = app_context.is_admin(user.id)

        is_start_command = bool(
            message
            and message.text
            and message.text.split(maxsplit=1)[0].split("@", 1)[0] == "/start"
        )
        if not is_admin and await self._rate_limit_rejected(event, message, user.id):
            return None

        session = app_context.open_session()
        try:
            service = app_context.service(session)
            chat_id = message.chat.id if message else (event.message.chat.id if event.message else user.id)
            banned = await service.record_user(user.id, chat_id, user.username, user.full_name)
        finally:
            await session.close()

        if banned and not is_admin:
            if message:
                await message.answer("هذا الحساب غير مخوّل باستخدام البوت حالياً.")
            else:
                await event.answer("هذا الحساب غير مخوّل باستخدام البوت حالياً.", show_alert=True)
            return None

        if app_context.settings.required_channel and bot and not is_start_command and not is_admin:
            is_member, membership_checked = await self._check_channel_membership(
                bot,
                app_context.settings.required_channel,
                user.id,
            )
            if not is_member:
                if membership_checked:
                    await self._revoke_referral(app_context, user.id, "channel_left")
                text = "للاستمرار، انضم إلى القناة الرسمية ثم اضغط «تحقق من الاشتراك»."
                if message:
                    await message.answer(text, reply_markup=subscription_menu(app_context.settings.required_channel_url))
                else:
                    await event.answer(text, show_alert=True)
                return None

        if message and not is_admin and bot and not is_start_command:
            monitoring = await app_context.monitoring_enabled_cached()
            if monitoring:
                await self._forward_to_admins(bot, app_context, message)

        return await handler(event, data)

    async def _check_channel_membership(
        self,
        bot: Bot,
        required_channel: str,
        user_id: int,
    ) -> tuple[bool, bool]:
        now = time.monotonic()
        if self._membership_cache.get(user_id, 0.0) > now:
            return True, False

        lock = self._membership_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._membership_locks[user_id] = lock
        async with lock:
            now = time.monotonic()
            if self._membership_cache.get(user_id, 0.0) > now:
                return True, False

            self._membership_cache.pop(user_id, None)
            try:
                membership = await bot.get_chat_member(required_channel, user_id)
            except Exception:
                logger.warning(
                    "تعذر التحقق من اشتراك المستخدم %s في القناة.",
                    user_id,
                    exc_info=True,
                )
                return False, False

            is_member = membership.status in {"member", "administrator", "creator"} or (
                membership.status == "restricted"
                and getattr(membership, "is_member", False)
            )
            if is_member:
                self._membership_cache[user_id] = (
                    time.monotonic() + MEMBERSHIP_CACHE_TTL_SECONDS
                )
            return is_member, True

    async def _rate_limit_rejected(
        self,
        event: Message | CallbackQuery,
        message: Message | None,
        user_id: int,
    ) -> bool:
        now = time.monotonic()
        self._cleanup_rate_state(now)
        if self._muted_until.get(user_id, 0) > now:
            if message:
                await self._warn_once(
                    message,
                    user_id,
                    now,
                    "يرجى التمهّل قليلاً قبل إرسال رسائل أخرى.",
                )
            else:
                if now - self._last_warning.get(user_id, 0) > 5:
                    self._last_warning[user_id] = now
                    await event.answer("يرجى التمهّل قليلاً قبل الضغط مجدداً.")
            return True

        if message and message.text and len(message.text) > MAX_MESSAGE_LENGTH:
            await message.answer(
                f"الرسالة طويلة جداً؛ الحد الأقصى هو {MAX_MESSAGE_LENGTH} محرفاً."
            )
            return True

        events = self._events[user_id]
        while events and now - events[0] > BURST_WINDOW_SECONDS:
            events.popleft()
        events.append(now)
        if len(events) > BURST_MAX_MESSAGES:
            self._muted_until[user_id] = now + MUTE_SECONDS
            events.clear()
            if message:
                await self._warn_once(
                    message,
                    user_id,
                    now,
                    "تم إيقاف استقبال رسائلك مؤقتاً بسبب الإرسال المتكرر.",
                )
            else:
                self._last_warning[user_id] = now
                await event.answer("تم إيقاف استقبال طلباتك مؤقتاً بسبب الضغط المتكرر.")
            return True
        return False

    def _cleanup_rate_state(self, now: float) -> None:
        if now - self._last_cleanup < RATE_STATE_CLEANUP_SECONDS:
            return
        self._last_cleanup = now
        for user_id, expires_at in list(self._membership_cache.items()):
            if expires_at <= now:
                self._membership_cache.pop(user_id, None)
        for user_id, events in list(self._events.items()):
            while events and now - events[0] > BURST_WINDOW_SECONDS:
                events.popleft()
            if not events and self._muted_until.get(user_id, 0) <= now:
                self._events.pop(user_id, None)
                self._muted_until.pop(user_id, None)
                self._last_warning.pop(user_id, None)

    async def _warn_once(self, message: Message, user_id: int, now: float, text: str) -> None:
        if now - self._last_warning.get(user_id, 0) > 5:
            self._last_warning[user_id] = now
            try:
                await message.answer(text)
            except Exception:
                logger.debug("Could not deliver flood warning to user %s", user_id, exc_info=True)

    async def _revoke_referral(
        self,
        app_context: ApplicationContext,
        referred_user_id: int,
        reason: str,
    ) -> None:
        session = app_context.open_session()
        try:
            service = app_context.service(session)
            revoke = getattr(service, "revoke_referral_for_referred_user", None)
            if revoke is None:
                logger.warning("خدمة إلغاء الإحالة غير متاحة حالياً.")
                return
            await revoke(referred_user_id, reason)
        except Exception:
            logger.exception("تعذر إلغاء إحالة المستخدم %s بسبب %s.", referred_user_id, reason)
        finally:
            await session.close()

    async def _forward_to_admins(self, bot: Bot, app_context: ApplicationContext, message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        username = f"@{message.from_user.username}" if message.from_user and message.from_user.username else "غير متوفر"
        full_name = message.from_user.full_name if message.from_user else "غير معروف"
        original_text = message.text or ""
        original_caption = message.caption or ""
        header = (
            "<b>رسالة واردة من المستخدم</b>\n"
            f"الاسم: <b>{html.escape(full_name)}</b>\n"
            f"username: <b>{html.escape(username)}</b>\n"
            f"معرّف المستخدم: <code>{user_id}</code>"
        )
        visible_header = (
            "رسالة واردة من المستخدم\n"
            f"الاسم: {full_name}\n"
            f"username: {username}\n"
            f"معرّف المستخدم: {user_id}"
        )
        reply_markup = monitoring_reply_menu(user_id)
        for admin_id in app_context.settings.admin_ids:
            try:
                if original_text:
                    visible_prefix = f"{visible_header}\n\nالرسالة الأصلية:\n"
                    content_limit = max(
                        1,
                        TELEGRAM_TEXT_LIMIT - _utf16_units(visible_prefix),
                    )
                    chunks = _split_by_utf16_units(original_text, content_limit)
                    for index, chunk in enumerate(chunks):
                        await bot.send_message(
                            admin_id,
                            f"{header}\n\n<b>الرسالة الأصلية:</b>\n{html.escape(chunk)}",
                            reply_markup=reply_markup if index == 0 else None,
                        )
                    continue

                media_caption = (
                    f"{header}\n\n<b>الرسالة الأصلية:</b>\n"
                    f"{html.escape(original_caption or '—')}"
                )
                visible_caption = (
                    f"{visible_header}\n\nالرسالة الأصلية:\n"
                    f"{original_caption or '—'}"
                )
                if _utf16_units(visible_caption) <= TELEGRAM_CAPTION_LIMIT:
                    sent = await self._send_media_card(
                        bot,
                        admin_id,
                        message,
                        media_caption,
                        reply_markup,
                    )
                    if sent:
                        continue

                # Unsupported/oversized captions are kept losslessly by putting
                # the original message directly below the labelled card.
                card = await bot.send_message(admin_id, header, reply_markup=reply_markup)
                await bot.copy_message(
                    admin_id,
                    message.chat.id,
                    message.message_id,
                    reply_to_message_id=card.message_id,
                )
            except Exception:
                logger.info("تعذر تمرير رسالة مستخدم إلى حساب الإدارة %s", admin_id, exc_info=True)

    async def _send_media_card(
        self,
        bot: Bot,
        admin_id: int,
        message: Message,
        caption: str,
        reply_markup: Any,
    ) -> bool:
        media_senders = (
            ("photo", "send_photo"),
            ("video", "send_video"),
            ("document", "send_document"),
            ("audio", "send_audio"),
            ("voice", "send_voice"),
            ("animation", "send_animation"),
        )
        for attribute, sender_name in media_senders:
            media = getattr(message, attribute, None)
            if not media:
                continue
            if attribute == "photo":
                media = media[-1].file_id
            else:
                media = media.file_id
            await getattr(bot, sender_name)(
                admin_id,
                media,
                caption=caption,
                reply_markup=reply_markup,
            )
            return True
        return False
