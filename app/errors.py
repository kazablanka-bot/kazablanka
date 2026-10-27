طimport logging
import traceback
from aiogram import Router, Bot
from aiogram.types import ErrorEvent

errors_router = Router()

@errors_router.error()
async def error_handler(event: ErrorEvent, bot: Bot, app_context):
    # طباعة الخطأ في الكونسول أولاً
    logging.exception(f"حدث خطأ في الـ Handler: {event.exception}")
    
    # جلب قائمة الأدمنز ديناميكياً من إعدادات البوت الخاص بك
    admin_ids = app_context.settings.admin_ids
    
    # استخراج تفاصيل استثناء البايثون بالكامل
    exc = event.exception
    tb = exc.__traceback__
    
    # جلب آخر مكان حدث فيه الخطأ (الملف، السطر، الدالة)
    summary = traceback.extract_tb(tb)
    if summary:
        last_trace = summary[-1]
        file_name = last_trace.filename.split('/')[-1] # اسم الملف فقط لسهولة القراءة
        line_number = last_trace.lineno
        func_name = last_trace.name
    else:
        file_name, line_number, func_name = "غير معروف", "غير معروف", "غير معروف"

    # جلب الـ Traceback الكامل (آخر 3 أسطر لتفادي تجاوز حد حجم رسالة تليغرام)
    full_traceback = "".join(traceback.format_exception(type(exc), exc, tb)[-3:])
    
    # محاولة معرفة من المستخدم وما هو التحديث الذي تسبب بالخطأ
    user_info = "غير معروف"
    update_type = "غير معروف"
    trigger_text = "لا يوجد نص"
    
    if event.update.message:
        update_type = "رسالة نصية (Message)"
        user = event.update.message.from_user
        user_info = f"@{user.username} ({user.id})" if user.username else f"{user.first_name} ({user.id})"
        trigger_text = event.update.message.text or "وسائط/أزرار"
    elif event.update.callback_query:
        update_type = "ضغط زر (Callback Query)"
        user = event.update.callback_query.from_user
        user_info = f"@{user.username} ({user.id})" if user.username else f"{user.first_name} ({user.id})"
        trigger_text = event.update.callback_query.data

    # صياغة رسالة تنبيهية احترافية ومنسقة بمظهر المطورين
    error_message = (
        "⚠️ **تنبيه: حدث خطأ غير معالج في البوت!**\n\n"
        f"👤 **المستخدم المتسبب:** `{user_info}`\n"
        f" نوع التحديث:** {update_type}\n"
        f" داتا التفعيل:** `{trigger_text}`\n\n"
        f"📁 **الملف المتسبب:** `{file_name}`\n"
        f"📍 **رقم السطر:** `{line_number}`\n"
        f"⚙️ **الدالة (Function):** `{func_name}`\n\n"
        f"💻 **تفاصيل تتبع الخطأ (Traceback):**\n"
        f"```python\n"
        f"{full_traceback}\n"
        f"```"
    )
    
    # إرسال التقرير التفصيلي لكل الأدمنز المسجلين في السيتينغ
    for admin_id in admin_ids:
        try:
            # تم استخدام HTML لتفادي مشاكل الأكواد والرموز الخاصة في الماركداون
            await bot.send_message(chat_id=admin_id, text=error_message, parse_mode="HTML")
        except Exception as e:
            logging.error(f"فشل إرسال التقرير التفصيلي للأدمن {admin_id}: {e}")
