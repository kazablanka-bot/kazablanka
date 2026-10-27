"""نقطة تشغيل البوت مع SQLite دائم أو PostgreSQL أو ذاكرة الاختبار."""

from __future__ import annotations
from app.services import get_sp_today_usd_rates

import asyncio
from app.errors import errors_router
import logging
import hashlib
import itertools
import sys
import contextvars
from app.errors import errors_router
import html

from aiogram.exceptions import TelegramRetryAfter


from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.context import ApplicationContext
from app.database import create_engine, create_session_factory, initialize_database
from app.handlers import restore_payment_expiry_tasks, router
from app.memory_store import MemoryService
from app.middleware import ActivityGuard

logger = logging.getLogger(__name__)

_IN_TELEGRAM_LOG_SEND = contextvars.ContextVar(
    "in_telegram_log_send",
    default=False,
)


class TelegramAdminLogHandler(logging.Handler):
    """يرسل التحذيرات والأخطاء المسجلة للأدمن بالتتابع."""

    ERROR_WORDS = (
        "حاول لاحقاً",
        "تعذر",
        "فشل",
        "خطأ",
        "error",
        "exception",
        "failed",
        "could not",
        "unable",
        "timeout",
    )

    def __init__(
        self,
        bot: Bot,
        admin_ids: list[int],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        super().__init__(level=logging.INFO)
        self.bot = bot
        self.admin_ids = admin_ids
        self.loop = loop
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._sequence = itertools.count(1)


    def emit(self, record: logging.LogRecord) -> None:
        if _IN_TELEGRAM_LOG_SEND.get():
            return

        try:
            message = record.getMessage()
            is_error_like_info = (
                record.levelno == logging.INFO
                and any(
                    word in message.casefold()
                    for word in self.ERROR_WORDS
                )
            )

            if record.levelno < logging.WARNING and not is_error_like_info:
                return

            exception_name = ""

            if record.exc_info and record.exc_info[1] is not None:
                exception = record.exc_info[1]
                exception_name = type(exception).__name__

            signature = "|".join(
                (
                    record.name,
                    record.funcName,
                    record.levelname,
                    str(record.msg),
                    exception_name,
                )
            )
            error_code = (
                "E-"
                + hashlib.sha256(
                    signature.encode("utf-8", "replace")
                ).hexdigest()[:6].upper()
            )
            event_number = next(self._sequence)

            timestamp = (
                self.formatter.formatTime(record, "%Y-%m-%d %H:%M:%S")
                if self.formatter
                else str(record.created)
            )

            parts = [
                f"#{event_number:06d} | {error_code} | {record.levelname}",
                f"{record.name}.{record.funcName}",
                f"{record.filename}:{record.lineno}",
                f"الوقت: {timestamp}",
                (
                    f"الملخص: استثناء من نوع {exception_name}."
                    if exception_name
                    else "الملخص: تنبيه تشغيلي؛ راجع سجل الخادم للتفاصيل."
                ),
                "التفاصيل الكاملة لا تُرسل عبر Telegram.",
            ]

            text = "\n".join(parts)
            self.loop.call_soon_threadsafe(self._enqueue, text)

        except Exception as error:
            sys.stderr.write(
                f"Could not queue Telegram admin log: {error}\n"
            )


    def _enqueue(self, text: str) -> None:
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            sys.stderr.write(
                "Telegram admin log queue is full; check the server console.\n"
            )

    async def run(self) -> None:
        while True:
            text = await self.queue.get()
            try:
                token = _IN_TELEGRAM_LOG_SEND.set(True)
                try:
                    safe_text = html.escape(text[:650])
                    for admin_id in self.admin_ids:
                        delivered = False
                        last_error: Exception | None = None

                        for attempt in range(4):
                            try:
                                await self.bot.send_message(
                                    admin_id,
                                    f"<pre>{safe_text}</pre>",
                                )
                                delivered = True
                                break
                            except TelegramRetryAfter as error:
                                last_error = error
                                await asyncio.sleep(error.retry_after + 0.5)
                            except Exception as error:
                                last_error = error
                                if attempt < 3:
                                    await asyncio.sleep(2 ** attempt)

                        if not delivered:
                            sys.stderr.write(
                                f"Could not send admin log to {admin_id}: "
                                f"{last_error}\n"
                            )

                    # يحدّ من معدل رسائل التنبيه؛ تبقى مرتبة في الطابور.
                    if self.admin_ids:
                        await asyncio.sleep(1.1)
                finally:
                    _IN_TELEGRAM_LOG_SEND.reset(token)
            finally:
                self.queue.task_done()



def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stdout,
    )

async def refresh_sp_today_rates_periodically() -> None:
    while True:
        try:
            await get_sp_today_usd_rates(force_refresh=True)
            logging.getLogger(__name__).info(
                "تم تحديث أسعار الدولار من الليرة اليوم."
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "تعذر تحديث أسعار الدولار من الليرة اليوم."
            )

        await asyncio.sleep(300)



async def main() -> None:
    try:
        settings = get_settings()
    except ValidationError as error:
        raise RuntimeError(
            "تحقق من إعدادات البوت المطلوبة، ومنها TELEGRAM_BOT_TOKEN وADMIN_IDS."
        ) from error

    configure_logging(settings.log_level)
    root_logger = logging.getLogger()
    # نحتاج INFO كي نلتقط رسائل الأخطاء القديمة المصنفة خطأً كمعلومات.
    root_logger.setLevel(min(root_logger.level, logging.INFO))

    bot = Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    loop = asyncio.get_running_loop()
    admin_log_handler = TelegramAdminLogHandler(
        bot=bot,
        admin_ids=settings.admin_ids,
        loop=loop,
    )
    admin_log_handler.setFormatter(
        logging.Formatter("%(asctime)s")
    )
    root_logger.addHandler(admin_log_handler)
    admin_log_task = asyncio.create_task(admin_log_handler.run())

    engine: AsyncEngine | None = None
    rates_refresh_task: asyncio.Task[None] | None = None

    try:
        if settings.storage_mode == "memory":
            memory_service = MemoryService(settings)
            await memory_service.bootstrap()
            app_context = ApplicationContext(
                settings=settings,
                memory_service=memory_service,
            )
            logger.warning(
                "Running with temporary in-memory storage; "
                "all data will be erased on restart."
            )
        else:
            if not settings.database_url:
                raise RuntimeError(
                    "DATABASE_URL مطلوبة عند STORAGE_MODE=postgresql."
                )
            engine = create_engine(settings.database_url)
            session_factory = create_session_factory(engine)
            app_context = ApplicationContext(
                settings=settings,
                session_factory=session_factory,
            )
            await initialize_database(engine)
            async with session_factory() as session:
                await app_context.service(session).bootstrap()
            logger.info(
                "Running with persistent database storage: %s",
                settings.storage_mode,
            )

        dispatcher = Dispatcher(
            storage=MemoryStorage(),
            app_context=app_context,
        )
        activity_guard = ActivityGuard()
        dispatcher.message.middleware(activity_guard)
        dispatcher.callback_query.middleware(activity_guard)
        dispatcher.include_router(router)
        dispatcher.include_router(errors_router)

        await bot.delete_webhook(drop_pending_updates=False)

        rates_refresh_task = asyncio.create_task(
            refresh_sp_today_rates_periodically()
        )

        await bot.set_my_commands(
            [
                BotCommand(command="start", description="بدء الاستخدام"),
                BotCommand(command="contact", description="تواصل معنا"),
            ],
            scope=BotCommandScopeDefault(),
        )

        for admin_id in settings.admin_ids:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="بدء الاستخدام"),
                    BotCommand(command="admin", description="لوحة التحكم"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )

        await restore_payment_expiry_tasks(bot, app_context, dispatcher)

        allowed_updates = set(dispatcher.resolve_used_update_types())
        allowed_updates.update({"chat_member", "my_chat_member"})

        await dispatcher.start_polling(
            bot,
            allowed_updates=sorted(allowed_updates),
            handle_signals=True,
            close_bot_session=False,
        )
    except Exception:
        logger.exception("Unhandled startup or polling failure")
        raise
    finally:
        if rates_refresh_task is not None:
            rates_refresh_task.cancel()
            await asyncio.gather(
                rates_refresh_task,
                return_exceptions=True,
            )

        try:
            await asyncio.wait_for(
                admin_log_handler.queue.join(),
                timeout=10,
            )
        except asyncio.TimeoutError:
            sys.stderr.write(
                "Timed out while flushing Telegram admin log queue.\n"
            )

        admin_log_task.cancel()
        await asyncio.gather(admin_log_task, return_exceptions=True)
        root_logger.removeHandler(admin_log_handler)
        admin_log_handler.close()

        await bot.session.close()

        if engine is not None:
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
