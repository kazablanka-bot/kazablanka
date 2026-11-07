import asyncio
import copy
import heapq
import json
import os
from pathlib import Path

from telethon import TelegramClient, types, utils
from telethon.errors import FloodWaitError

# أدخل بيانات التطبيق محليًا فقط من https://my.telegram.org
API_ID = 1234567
API_HASH = "PUT_YOUR_API_HASH_HERE"

# أضيفي هنا القنوات العشر أو أي عدد آخر. للقنوات الخاصة استخدمي ID بصيغة -100...
SOURCE_CHANNELS = [
    -1001111111111,
    -1002222222222,
    # أضيفي بقية القنوات هنا...
]

# وجهة واحدة فقط. يجب أن يكون الحساب مخولًا بالنشر فيها.
TARGET_CHANNEL = -1003333333333

# ابدئي بالمعاينة فقط. بعد مراجعتها غيّري إلى False لتشغيل النقل.
DRY_RUN = True

# عدد الوحدات للمعاينة/التجربة الإجمالية من كل المصادر مجتمعة.
# الوحدة = منشور عادي أو ألبوم كامل. بعد تجربة ناجحة غيّري القيمة إلى None.
MAX_UNITS_PER_RUN = 5

# بعد نقل الأرشيف، افحص المنشورات الجديدة دوريًا. يعمل فقط عندما تكون MAX_UNITS_PER_RUN=None.
FOLLOW_NEW_POSTS = True
POLL_INTERVAL_SECONDS = 20
DELAY_BETWEEN_UNITS_SECONDS = 1.5

# ملف الاستئناف ينشأ بجانب السكربت. لا تحذفيه ولا تعدّلي القنوات/الوجهة أثناء النقل.
STATE_FILE = Path(__file__).with_name("telegram_migration_state.json")
ORDERING_VERSION = "global_chronological_v1"


def source_key(source) -> str:
    return str(source)


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def bold_only_entities(text: str, original_entities=None):
    """يجعل النص كله عريضًا، ويبقي الروابط والمنشنات القابلة للنقر فقط."""
    if not text:
        return []
    entities = [types.MessageEntityBold(offset=0, length=utf16_length(text))]
    clickable = {
        "MessageEntityTextUrl",
        "MessageEntityUrl",
        "MessageEntityMention",
        "MessageEntityMentionName",
        "InputMessageEntityMentionName",
        "MessageEntityBotCommand",
        "MessageEntityEmail",
        "MessageEntityHashtag",
        "MessageEntityCashtag",
    }
    for entity in original_entities or []:
        if entity.__class__.__name__ in clickable:
            entities.append(copy.copy(entity))
    return entities


def load_state():
    expected_sources = [source_key(source) for source in SOURCE_CHANNELS]
    if not STATE_FILE.exists():
        return {
            "target": str(TARGET_CHANNEL),
            "sources": expected_sources,
            "ordering": ORDERING_VERSION,
            "last_ids": {},
        }

    with STATE_FILE.open("r", encoding="utf-8") as handle:
        state = json.load(handle)

    if state.get("target") != str(TARGET_CHANNEL):
        raise RuntimeError("ملف الاستئناف مرتبط بوجهة مختلفة؛ أوقفي التشغيل ولا تغيّري الهدف.")
    if state.get("ordering") != ORDERING_VERSION:
        raise RuntimeError(
            "ملف الاستئناف من نسخة أقدم أو بترتيب مختلف. لا تحذفيه إذا نُقلت منشورات فعلية؛ "
            "راجعي محتوى الهدف أولًا. إذا كانت الوجهة ما زالت فارغة، أعيدي تسمية ملف الحالة ثم ابدئي معاينة جديدة."
        )
    if state.get("sources") != expected_sources:
        raise RuntimeError(
            "قائمة المصادر لا تطابق القائمة المسجلة في ملف الاستئناف. "
            "استخدمي نفس القنوات والترتيب حتى لا يحدث تكرار أو فقدان."
        )
    state.setdefault("last_ids", {})
    return state


def save_state(state):
    temp_file = STATE_FILE.with_suffix(".tmp")
    with temp_file.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_file, STATE_FILE)


def media_kind_and_input(message):
    """يقبل الصور والفيديو فقط ويعيد InputMedia مع تفعيل spoiler."""
    media = message.media
    if isinstance(media, types.MessageMediaPhoto):
        kind = "صورة"
    elif isinstance(media, types.MessageMediaDocument):
        document = getattr(media, "document", None)
        attrs = getattr(document, "attributes", []) or []
        if any(isinstance(attr, types.DocumentAttributeVideo) for attr in attrs):
            kind = "فيديو"
        else:
            raise ValueError(
                f"الرسالة {message.id}: تحتوي ملفًا ليس صورة أو فيديو؛ "
                "أوقفت النقل حتى لا يُرسل عنصر غير مموّه."
            )
    else:
        raise ValueError(
            f"الرسالة {message.id}: نوع الوسيط غير مدعوم للتمويه التلقائي "
            f"({type(media).__name__}). أوقفت النقل دون تخطيه."
        )

    return kind, message


async def iter_units(client, source, min_id: int):
    """يقرأ مصدرًا تصاعديًا ويجمع عناصر كل ألبوم كوحدة واحدة."""
    pending = []
    pending_group_id = None

    async for message in client.iter_messages(source, reverse=True, min_id=min_id):
        group_id = message.grouped_id
        if not pending:
            pending = [message]
            pending_group_id = group_id
        elif group_id is not None and group_id == pending_group_id:
            pending.append(message)
        else:
            yield pending
            pending = [message]
            pending_group_id = group_id

    if pending:
        yield pending


async def merged_units(client, state):
    """دمج مؤشرات المصادر العشر وترتيب المنشورات حسب تاريخها، مع إبقاء الألبومات مجتمعة."""
    iterators = []
    heap = []

    for source_index, source in enumerate(SOURCE_CHANNELS):
        min_id = int(state["last_ids"].get(source_key(source), 0))
        iterator = iter_units(client, source, min_id).__aiter__()
        iterators.append(iterator)
        try:
            unit = await iterator.__anext__()
        except StopAsyncIteration:
            continue
        # عند تعادل الوقت: ترتيب قائمة المصادر، ثم رقم أول رسالة.
        heapq.heappush(
            heap,
            (unit[0].date.timestamp(), source_index, unit[0].id, source, unit),
        )

    while heap:
        _, source_index, _, source, unit = heapq.heappop(heap)
        yield source, unit
        try:
            next_unit = await iterators[source_index].__anext__()
        except StopAsyncIteration:
            continue
        heapq.heappush(
            heap,
            (
                next_unit[0].date.timestamp(),
                source_index,
                next_unit[0].id,
                source,
                next_unit,
            ),
        )


def describe_unit(source, unit):
    first_id, last_id = unit[0].id, unit[-1].id
    media_count = sum(bool(message.media) for message in unit)
    if unit[0].grouped_id is not None:
        label = f"ألبوم كامل ({len(unit)} عناصر)"
    else:
        label = "منشور"
    date = unit[0].date.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"{date} | المصدر {source} | {label} | IDs {first_id}-{last_id} "
        f"| وسائط: {media_count}"
    )


async def send_unit(client, unit):
    if len(unit) > 10:
        raise ValueError(
            f"مجموعة الرسائل {unit[0].grouped_id} تضم أكثر من 10 عناصر؛ "
            "أوقفتها بدل تقسيم الألبوم أو تغيير شكله."
        )

    media_messages = [message for message in unit if message.media]
    if media_messages:
        if len(media_messages) != len(unit):
            raise ValueError(
                f"الوحدة عند الرسالة {unit[0].id} تجمع نصًا ووسائط بشكل غير متوقع؛ "
                "أوقفت النقل للمراجعة."
            )

        media_inputs, captions, caption_entities = [], [], []
        import io # لاستخدام الذاكرة المؤقتة

        for message in unit:
            kind, msg_obj = media_kind_and_input(message)
            
            # تحميل الملف بالكامل إلى الذاكرة لتخطي حظر القنوات المقيدة
            file_bytes = io.BytesIO()
            await client.download_media(msg_obj, file_bytes)
            file_bytes.seek(0)
            
            # تحديد اسم افتراضي للملف ليعرف تلغرام نوعه (صورة أو فيديو)
            file_bytes.name = "image.jpg" if kind == "صورة" else "video.mp4"
            
            # استخدام InputMediaUploadedPhoto أو InputMediaUploadedDocument لتفعيل الـ spoiler على الملف المرفوع حديثاً
            uploaded_file = await client.upload_file(file_bytes)
            if kind == "صورة":
                input_media = types.InputMediaUploadedPhoto(file=uploaded_file, spoiler=True)
            else:
                # جلب الخصائص الأصلية للفيديو (مثل الأبعاد والمدة) إن وجدت لضمان جودته وشكله
                video_attributes = []
                if hasattr(msg_obj.media, 'document') and msg_obj.media.document:
                    video_attributes = [attr for attr in msg_obj.media.document.attributes if isinstance(attr, types.DocumentAttributeVideo)]
                input_media = types.InputMediaUploadedDocument(
                    file=uploaded_file, 
                    mime_type="video/mp4", 
                    attributes=video_attributes,
                    spoiler=True
                )

            media_inputs.append(input_media)
            caption = message.message or ""
            captions.append(caption)
            caption_entities.append(bold_only_entities(caption, message.entities))
            print(f"    تم تحميل وتهيئة عنصر {message.id}: {kind} | spoiler=ON")

        # إرسال الملفات المرفوعة حديثاً كألبوم أو كمنشور منفرد
        await client.send_file(
            TARGET_CHANNEL,
            media_inputs if (len(unit) > 1 or unit[0].grouped_id is not None) else media_inputs[0],
            caption=captions if (len(unit) > 1 or unit[0].grouped_id is not None) else captions[0],
            formatting_entities=caption_entities if (len(unit) > 1 or unit[0].grouped_id is not None) else caption_entities[0],
        )
        return

    if len(unit) != 1 or not unit[0].message:
        # رسائل الخدمة بلا نص أو وسائط لا تُنشر.
        return

    message = unit[0]
    await client.send_message(
        TARGET_CHANNEL,
        message.message,
        formatting_entities=bold_only_entities(message.message, message.entities),
        link_preview=False,
    )


async def main():
    if API_ID == 1234567 or API_HASH == "PUT_YOUR_API_HASH_HERE":
        raise ValueError("أدخلي API_ID وAPI_HASH محليًا قبل التشغيل.")
    if not SOURCE_CHANNELS:
        raise ValueError("أضيفي قناة مصدر واحدة على الأقل إلى SOURCE_CHANNELS.")
    if len({source_key(x) for x in SOURCE_CHANNELS}) != len(SOURCE_CHANNELS):
        raise ValueError("هناك قناة مصدر مكررة في SOURCE_CHANNELS.")
    if source_key(TARGET_CHANNEL) in {source_key(x) for x in SOURCE_CHANNELS}:
        raise ValueError("لا يمكن أن تكون قناة الهدف ضمن قائمة المصادر.")

    if not DRY_RUN:
        print("وضع النقل الفعلي مفعّل.")
        if input("بعد مراجعة الإعدادات، اكتبي COPY للبدء: ").strip() != "COPY":
            print("أُلغي التشغيل دون نقل.")
            return
    else:
        print("وضع المعاينة فقط: لن يُرسل أي منشور.")

    state = load_state()
    async with TelegramClient("telegram_channel_migrator", API_ID, API_HASH) as client:
        await client.get_entity(TARGET_CHANNEL)
        for source in SOURCE_CHANNELS:
            await client.get_entity(source)

        while True:
            processed = 0
            had_pending = False

            async for source, unit in merged_units(client, state):
                had_pending = True
                print("\n" + describe_unit(source, unit))
                for message in unit:
                    if message.message:
                        print("النص:", message.message.replace("\n", " ⏎ ")[:400])
                    if message.media:
                        kind, _ = media_kind_and_input(message)
                        print(f"الوسيط: {kind} (سيُرسل مموّهًا)")

                if not DRY_RUN:
                    while True:
                        try:
                            await send_unit(client, unit)
                            break
                        except FloodWaitError as error:
                            wait_seconds = error.seconds + 2
                            print(
                                f"طلب تيليجرام الانتظار {error.seconds} ثانية؛ "
                                f"سأنتظر {wait_seconds} ثانية ثم أعيد المحاولة."
                            )
                            await asyncio.sleep(wait_seconds)

                    # حفظ التقدم لهذا المصدر بعد إرسال المنشور/الألبوم كاملًا.
                    state["last_ids"][source_key(source)] = unit[-1].id
                    save_state(state)
                    await asyncio.sleep(DELAY_BETWEEN_UNITS_SECONDS)

                processed += 1
                if MAX_UNITS_PER_RUN is not None and processed >= MAX_UNITS_PER_RUN:
                    break

            if DRY_RUN:
                print(
                    f"\nانتهت المعاينة: {processed} وحدات بترتيب زمني عالمي. "
                    "لم يتم إرسال أي شيء."
                )
                return

            if MAX_UNITS_PER_RUN is not None and processed >= MAX_UNITS_PER_RUN:
                print(
                    "\nانتهت دفعة التجربة. لإكمال النقل، غيّري MAX_UNITS_PER_RUN إلى None "
                    "وشغّلي السكربت ثانية؛ سيكمل باستخدام ملف الاستئناف."
                )
                return

            if not FOLLOW_NEW_POSTS:
                print("اكتمل نقل الأرشيف. انتهى السكربت.")
                return

            if not had_pending:
                print(f"لا توجد منشورات جديدة. إعادة الفحص بعد {POLL_INTERVAL_SECONDS} ثانية.")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
