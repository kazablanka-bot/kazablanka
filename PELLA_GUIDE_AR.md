# دليل النشر والإعداد

## ملف البيئة

ضع القيم التالية في `.env` بجانب `main.py`، وأبقِ رمز البوت فارغاً هنا حتى تضيفه بنفسك. لا ترفع `.env` أو قاعدة البيانات إلى GitHub.

```dotenv
TELEGRAM_BOT_TOKEN=
ADMIN_IDS=123456789

STORAGE_MODE=sqlite
DATABASE_URL=

REQUIRED_CHANNEL=@your_channel
REQUIRED_CHANNEL_URL=https://t.me/your_channel

DEFAULT_SYP_PER_USD=140
DEFAULT_USDT_PER_USD=1
MINIMUM_USD=100
DEFAULT_USD_TO_USDT_FEE=1.5
DEFAULT_USDT_TO_USD_FEE=1.5

QUOTE_TTL_MINUTES=15
REVIEW_TTL_MINUTES=5
COMPLETION_ETA=من 5 إلى 10 دقائق

REFERRAL_SIGNUP_BONUS=1
REFERRAL_LEVEL_1_REWARD=0
REFERRAL_LEVEL_2_REWARD=0
REFERRAL_LEVEL_3_REWARD=0
REFERRAL_LEVEL_4_REWARD=0

WITHDRAWAL_MINIMUM_USD=50
WITHDRAWAL_REVIEW_HOURS=24

CONTACT_URL=https://t.me/your_contact
LOG_LEVEL=INFO
```

معرّفات المستويات ثابتة: الأول من 0 إلى 20 إحالة، الثاني من 21 إلى 50، الثالث من 51 إلى 100، والرابع من 101 فأكثر. عدّل قيم `REFERRAL_LEVEL_*_REWARD` يدوياً حسب سياستك؛ القيمة `0` تعني عدم منح مكافأة إحالة لذلك المستوى. مكافأة التسجيل مستقلة وتبقى دولاراً واحداً افتراضياً.

احذف من ملف البيئة القديم `REFERRAL_BEGINNER_REWARD` و`REFERRAL_ADVANCED_REWARD` و`REFERRAL_EXPERT_REWARD` و`WEEKLY_TOP_BONUS_USD`؛ لم تعد هذه المفاتيح مستخدمة، كما أُزيل الترتيب والجائزة الأسبوعيان من البوت.

عند `STORAGE_MODE=sqlite` يُنشأ `databases.db` تلقائياً إذا تركت `DATABASE_URL` فارغاً. للنشر الدائم استخدم PostgreSQL واضبط `STORAGE_MODE=postgresql` و`DATABASE_URL` بعنوان اتصال PostgreSQL الخاص بخدمة الاستضافة.

## القناة والإحالات

اضبط `REQUIRED_CHANNEL` باسم القناة أو معرّفها و`REQUIRED_CHANNEL_URL` كرابط الانضمام. حتى يتحقق البوت من عضوية المستخدم ويرصد مغادرته تلقائياً، أضفه مشرفاً في القناة المطلوبة. يطلب التطبيق تحديثات `chat_member` و`my_chat_member` من Telegram؛ راجع [توثيق Telegram Bot API](https://core.telegram.org/bots/api#update).

تُحسب الإحالات التي تبدأ بعد تفعيل البرنامج الجديد فقط. مغادرة القناة أو حظر المستخدم للبوت يلغي الإحالة نهائياً، ويُعكس الجزء غير المسحوب من مكافأتها فقط. لا يمكن استرداد مكافأة سبق دفعها عبر سحب مكتمل، ولا يُعاد احتساب الإحالة إذا عاد المستخدم لاحقاً.

## التشغيل وأمان الإنتاج

للاختبار المحلي، استخدم بيئة Python واعتماديات `requirements.txt` ثم شغّل `python main.py`. لبيانات حقيقية استخدم PostgreSQL، وتأكد من اتصال الخدمة الصادر إلى Telegram Bot API، وشغّل نسخة polling واحدة فقط بالرمز نفسه.

قبل استقبال أموال أو تنفيذ قرارات مالية فعلية: دوّر أي رمز بوت أو كلمة مرور سبق نشرها، واختبر نسخة staging باستخدام بوت وقناة وقاعدة بيانات منفصلة، وتحقق من خطة النسخ الاحتياطي والاستعادة وإشعارات قرارات السحب. نجاح الاختبارات المحلية لا يثبت وحده سلامة كل سيناريوهات Telegram أو PostgreSQL في الإنتاج.
