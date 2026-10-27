
import logging
import traceback
from aiogram import Router, Bot
from aiogram.types import ErrorEvent

# ⚠️ ضع هنا الآيدي الخاص بك كأدمن (رقمي فقط بدون علامات تنصيص)
ADMIN_ID = 123456789  

errors_router = Router()

@errors_router.error()
async def error_handler(event: ErrorEvent, bot: Bot):
    logging.exception(f"حدث خطأ غير متوقع: {event.exception}")
    
    # جلب آخر 3 أسطر من تفاصيل الخطأ وتتبع السطر لسهولة القراءة
    error_traceback = "".join(traceback.format_exception(None, event.exception, event.exception.__traceback__)[-3:])
    
    error_message = (
        "⚠️ *تنبيه: حدث خطأ غير متوقع في البوت!*\n\n"
        f"💻 *تفاصيل الخطأ ورقم السطر:*\n"
        f"```python\n{error_traceback}\n```"
    )
    
    try:
        # استخدام التنسيق Markdown العادي لضمان استقرار الرسالة
        await bot.send_message(chat_id=ADMIN_ID, text=error_message, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"فشل إرسال تقرير الخطأ للمطور/الأدمن: {e}")
