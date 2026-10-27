"""لوحات أزرار عربية لواجهة العميل ولوحة الإدارة."""

from __future__ import annotations

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from app.models import Currency, ExchangeRoute, RouteCode

ROUTE_LABELS: dict[RouteCode, str] = {
    RouteCode.SYP_TO_USD: "سأرسل شام كاش ليرة، وأستلم شام كاش دولار.",
    RouteCode.USD_TO_SYP: "سأرسل شام كاش دولار، وأستلم شام كاش ليرة.",
    RouteCode.SYP_TO_USDT: "سأرسل شام كاش ليرة، وأستلم عملات رقمية.",
    RouteCode.USD_TO_USDT: "سأرسل شام كاش دولار، وأستلم عملات رقمية.",
    RouteCode.USDT_TO_SYP: "سأرسل عملات رقمية، وأستلم شام كاش ليرة.",
    RouteCode.USDT_TO_USD: "سأرسل عملات رقمية، وأستلم شام كاش دولار.",
}

CRYPTO_METHOD_LABELS = {
    "TRC20": "USDT — TRC20",
    "ERC20": "USDT — ERC20",
    "BEP20": "USDT — BEP20",
    "TON": "USDT — TON",
    "CWALLET": "Cwallet مباشر",
    "BINANCE": "Binance مباشر",
}

def rates_refresh_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="♻️ تـحـديـث الأسـعـار ♻️", callback_data="menu:rates")] 
    ])

def main_menu(
    is_admin: bool,
    contact_url: str | None = None,
    include_rates_refresh: bool = False,
) -> InlineKeyboardMarkup:
    if is_admin:
        return admin_home_menu()

    rows = []

    if include_rates_refresh:
        rows.append([
            InlineKeyboardButton(
                text="♻️ تـحـديـث الأسـعـار ♻️",
                callback_data="menu:rates",
                style="primary",
            )
        ])

    rows.extend([
        [
            InlineKeyboardButton(
                text="🏧 إنـشـاء طـلـب تـحـويـل 🏧",
                callback_data="menu:convert",
                style="success",
            )
        ],
        [
            InlineKeyboardButton(
                text="💡 قـارن وعـايـن 🔍",
                callback_data="menu:compare",
            ),
            InlineKeyboardButton(
                text="🧮 احـسـب هـنـا ⚡",
                callback_data="menu:calculator",
            ),
        ],
        [
            InlineKeyboardButton(
                text="💰💵 الـربـح والـكـسـب الآمـن 💵💰",
                callback_data="menu:referrals",
            )
        ],
        [
            InlineKeyboardButton(
                text="📋 سـجـل طـلـبـاتـي 📋",
                callback_data="menu:requests",
            ),
            InlineKeyboardButton(
                text="📩🛠 تـواصـل مـعـنـا 🛠📩",
                url=contact_url,
            ) if contact_url else InlineKeyboardButton(
                text="تواصل معنا",
                callback_data="menu:contact",
            ),
        ],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def start_only_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="ابدأ",
                    callback_data="menu:home",
                )
            ]
        ]
    )
def subscription_menu(channel_url: str | None) -> InlineKeyboardMarkup:
    """زر الانضمام والتحقق؛ تُستخدم عند ضبط القناة الإلزامية فقط."""
    rows = []
    if channel_url:
        rows.append([InlineKeyboardButton(text="🔰 الإنـضـمـام إلـى الـقـنـاة الـرسـمـيـة 🔰", url=channel_url)])
    rows.append([InlineKeyboardButton(text="✅ الـتـحـقّـق مِـن الإشـتـراك ✅", callback_data="subscription:check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wallet_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 تـحـديـث الـمـحـفـظـة 🔄", callback_data="menu:wallet")],
        [InlineKeyboardButton(text="↪️ الــعــودة ↪️", callback_data="menu:referrals")],
    ])


def referrals_menu() -> InlineKeyboardMarkup:
    """Navigation for referral, wallet, rewards, and withdrawal actions."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📑 مـحـفـظـتـي ومـعـلـومـاتـي 💳", callback_data="menu:wallet")],
        [InlineKeyboardButton(text="🔗 رابـط إحـالـتـك 🔗", callback_data="menu:referral")],
        [InlineKeyboardButton(text="💵 دلـيـل أربـاحـك 💵", callback_data="menu:rewards")],
        [InlineKeyboardButton(text="💳 حـسـاب الـسـحـب 💳", callback_data="withdrawal:account")],
        [InlineKeyboardButton(text="💰 سـحـب أربـاحـك 💰", callback_data="menu:withdrawal")],
        [InlineKeyboardButton(text="↪️ العودة إلى القائمة الرئيسية ↪️", callback_data="menu:home")],
    ])


def withdrawal_method_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="● شام كاش دولار ●", callback_data="withdrawal:method:ShamCash USD")],
        [InlineKeyboardButton(text="● شام كاش ليرة ●", callback_data="withdrawal:method:ShamCash SYP")],
        [InlineKeyboardButton(text="● عملات رقمية ●", callback_data="withdrawal:method:Crypto")],
        [InlineKeyboardButton(text="❌ إلـغـاء الـعـمـلـيـة ❌", callback_data="flow:cancel")],
    ])


def withdrawal_confirm_menu(public_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تـأكـيـد طـلـب الـسـحـب ✅", callback_data=f"withdrawal:confirm:{public_id}")],
        [InlineKeyboardButton(text="❌ إلـغـاء الـعـمـلـيـة ❌", callback_data="flow:cancel")],
    ])


def routes_menu(routes: list[ExchangeRoute]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=ROUTE_LABELS[route.code], callback_data=f"route:{route.code.value}")]
            for route in routes]
    rows.append([InlineKeyboardButton(text="↪️ العودة إلى القائمة الرئيسية ↩️", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def quick_calculator_mode_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="لـدي مـبـلـغ لـلإرسـال", callback_data="quickcalc:mode:send")],
        [InlineKeyboardButton(text="اريـد اسـتـلام مـبـلـغ مـحـدد", callback_data="quickcalc:mode:receive")],
        [InlineKeyboardButton(text="↪️ العودة إلى القائمة الرئيسية ↩️", callback_data="menu:home")],
    ])


def quick_calculator_routes_menu(routes: list[ExchangeRoute | RouteCode]) -> InlineKeyboardMarkup:
    labels = {
        RouteCode.SYP_TO_USD: "أرسل ليرة، استلم دولار",
        RouteCode.USD_TO_SYP: "أرسل دولار، استلم ليرة",
        RouteCode.SYP_TO_USDT: "أرسل ليرة، استلم USDT",
        RouteCode.USD_TO_USDT: "أرسل دولار، استلم USDT",
        RouteCode.USDT_TO_SYP: "أرسل USDT، استلم ليرة",
        RouteCode.USDT_TO_USD: "أرسل USDT، استلم دولار",
    }
    rows = []
    for route in routes:
        route_code = route if isinstance(route, RouteCode) else route.code
        rows.append([InlineKeyboardButton(
            text=labels[route_code],
            callback_data=f"quickcalc:route:{route_code.value}",
        )])
    rows.append([InlineKeyboardButton(text="↪️ العودة إلى القائمة الرئيسية ↩️", callback_data="quickcalc:change-mode")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def quick_calculator_result_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="تـغـيـيـر الـطـلـب", callback_data="quickcalc:change-route"),
            InlineKeyboardButton(text="تـغـيـير الـطـريـقـة", callback_data="quickcalc:change-mode"),
        ],
        [InlineKeyboardButton(text="↪️ العودة إلى القائمة الرئيسية ↩️", callback_data="menu:home")],
    ])


def comparison_source_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="ليرة سورية (شام كاش)",
            callback_data="compare:source:SYP",
        )],
        [InlineKeyboardButton(
            text="دولار (شام كاش و USDT)",
            callback_data="compare:source:USD",
        )],
        [InlineKeyboardButton(
            text="↪️ العودة إلى القائمة الرئيسية ↩️",
            callback_data="menu:home",
        )],
    ])


def receive_currency_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 أحـتـاج ● شـام كـاش ل.س ● 📥", callback_data="convert:to:SYP")],
        [InlineKeyboardButton(text="📥 أحـتـاج ● شـام كـاش دولار ● 📥", callback_data="convert:to:USD")],
        [InlineKeyboardButton(text="📥 أحـتـاج ● عـمـلات رقــمـيـة ● 📥", callback_data="convert:to:USDT")],
        [InlineKeyboardButton(text="↪️ العودة إلى القائمة الرئيسية ↪️", callback_data="menu:home")],
    ])


def send_currency_menu(routes: list[ExchangeRoute]) -> InlineKeyboardMarkup:
    currency_names = {
        Currency.SYP: "● شـام كـاش ل.س ● 📤",
        Currency.USD: "● شـام كـاش دولار ● 📤",
        Currency.USDT: "● عـمـلات رقــمـيـة ● 📤",
    }

    rows = [
        [InlineKeyboardButton(
            text=f"📤 سـأُرسـل {currency_names[route.from_currency]}",
            callback_data=f"route:{route.code.value}",
        )]
        for route in routes
    ]
    rows.append([
        InlineKeyboardButton(
            text="↪️ رجوع لاختيار عملة الاستلام ↪️",
            callback_data="menu:convert",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلـغـاء الـعـمـلـيـة ❌", callback_data="flow:cancel")]])


def target_calculator_menu(
    preview: bool = False,
    mode: str = "receive",
    show_modes: bool = True,
) -> InlineKeyboardMarkup:
    if mode not in {"receive", "send"}:
        mode = "receive"

    rows = []

    if preview:
        rows = [
            [
                InlineKeyboardButton(
                    text="⬅️ الـمـتـابـعـة و تـثـبـيـت الـطـلـب ⬅️",
                    callback_data="quote:continue",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️تـغـيـيـر الـمـبـلـغ✏️",
                    callback_data="quote:change",
                ),
                InlineKeyboardButton(
                    text="♻️ تـحـديـث الـسـعـر ♻️",
                    callback_data="quote:refresh",
                ),
            ],
        ]
    elif show_modes:
        rows.append([
            InlineKeyboardButton(
                text="اريـد اسـتـلام مـبـلـغ مـحـدد",
                callback_data="quote:mode:receive",
            ),
            InlineKeyboardButton(
                text="لـدي مـبـلـغ لـلإرسـال",
                callback_data="quote:mode:send",
            ),
        ])

    rows.append([
        InlineKeyboardButton(
            text="❌ إلـغـاء الـعـمـلـيـة ❌",
            callback_data="flow:cancel",
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)



def admin_edit_menu(route_code: RouteCode | None = None) -> InlineKeyboardMarkup:
    rows = []

    if route_code is not None:
        rows.append([
            InlineKeyboardButton(
                text="رجوع إلى إعدادات المسار",
                callback_data=f"admin:route:{route_code.value}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="إلغاء التعديل والعودة إلى لوحة التحكم",
            callback_data="admin:home",
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)
    
def crypto_methods_menu() -> InlineKeyboardMarkup:
    methods = list(CRYPTO_METHOD_LABELS.items())
    rows = []

    for index in range(0, len(methods), 2):
        pair = methods[index:index + 2]
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"admin:crypto-method:{method}",
            )
            for method, label in pair
        ])

    rows.append([
        InlineKeyboardButton(
            text="العودة إلى لوحة التحكم",
            callback_data="admin:home",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def crypto_method_detail_menu(method: str) -> InlineKeyboardMarkup:
    if method not in CRYPTO_METHOD_LABELS:
        raise ValueError("طريقة USDT غير معروفة.")

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="تعديل العنوان أو الحساب",
                callback_data=f"admin:crypto-address:{method}",
            )
        ],
        [
            InlineKeyboardButton(
                text="تعديل صورة QR",
                callback_data=f"admin:crypto-photo:{method}",
            )
        ],
        [
            InlineKeyboardButton(
                text="العودة إلى طرق USDT",
                callback_data="admin:crypto-methods",
            )
        ],
        [
            InlineKeyboardButton(
                text="العودة إلى لوحة التحكم",
                callback_data="admin:home",
            )
        ],
    ])

def user_crypto_method_menu() -> InlineKeyboardMarkup:
    methods = list(CRYPTO_METHOD_LABELS.items())
    rows = []
 
    for index in range(0, len(methods), 2):
        pair = methods[index:index + 2]
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"crypto:choose:{method}",
            )
            for method, label in pair
        ])
 
    rows.append([
        InlineKeyboardButton(
            text="❌ إلـغـاء الـعـمـلـيـة ❌",
            callback_data="flow:cancel",
        )
    ])
 
    return InlineKeyboardMarkup(inline_keyboard=rows)
 
def payment_menu(payment_address: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="نـسـخ عـنـوان الـدفـع 🔗", copy_text=CopyTextButton(text=payment_address))],
        [InlineKeyboardButton(text="❌إلـغـاء الـعـمـلـيـة❌", callback_data="flow:cancel")],
    ])


def admin_home_menu(monitoring: bool | None = None) -> InlineKeyboardMarkup:
    monitoring_label = (
        "تبديل وضع المتابعة"
        if monitoring is None
        else "إيقاف وضع المتابعة" if monitoring else "تفعيل وضع المتابعة"
    )

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="الطلبات قيد المراجعة",
                callback_data="admin:pending",
            ),
            InlineKeyboardButton(
                text="طلبات السحب",
                callback_data="admin:withdrawals",
            ),
        ],
        [
            InlineKeyboardButton(
                text="المسارات وبيانات الدفع",
                callback_data="admin:routes",
            ),
            InlineKeyboardButton(
                text="طرق دفع USDT",
                callback_data="admin:crypto-methods",
            ),
        ],
        [
            InlineKeyboardButton(
                text="الإحالات",
                callback_data="admin:referrals",
            ),
            InlineKeyboardButton(
                text="إحصاءات البوت",
                callback_data="admin:stats",
            ),
        ],
        [
            InlineKeyboardButton(
                text="حظر أو رفع الحظر",
                callback_data="admin:ban",
            ),
            InlineKeyboardButton(
                text="رسالة أو إعلان",
                callback_data="admin:broadcast",
            ),
        ],
        [
            InlineKeyboardButton(
                text="قيم الأسعار الاحتياطية",
                callback_data="admin:rates",
            ),
        ],
        [
            InlineKeyboardButton(
                text=monitoring_label,
                callback_data="admin:monitor-toggle",
            ),
        ],
        [
            InlineKeyboardButton(
                text="العودة إلى القائمة الرئيسية",
                callback_data="menu:home",
            ),
        ],
    ])


def request_confirmation_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تـأكـيـد وإرسـال الـطـلـب ✅", callback_data="request:confirm", style="success")],
        [InlineKeyboardButton(text="✏️ تـعــديـل الـبـيـانـات ✏️", callback_data="request:edit", style="primary")],
        [InlineKeyboardButton(text="❌ إلـغـاء الـعـمـلـيـة ❌", callback_data="request:cancel", style="danger")],
    ])
    

def rates_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="تعديل قيمة التهيئة الاحتياطية لليرة",
                callback_data="admin:rate:syp_per_usd",
            )
        ],
        [
            InlineKeyboardButton(
                text="تعديل قيمة التهيئة الاحتياطية لـ USDT",
                callback_data="admin:rate:usdt_per_usd",
            )
        ],
        [
            InlineKeyboardButton(
                text="العودة إلى لوحة التحكم",
                callback_data="admin:home",
            )
        ],
    ])


def route_settings_menu(routes: list[ExchangeRoute]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=ROUTE_LABELS[route.code], callback_data=f"admin:route:{route.code.value}")]
            for route in routes]
    rows.append([InlineKeyboardButton(text="العودة إلى لوحة التحكم", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def route_detail_menu(route: ExchangeRoute) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=("إيقاف المسار" if route.is_enabled else "تفعيل المسار"),
                callback_data=(
                    f"admin:toggle:{route.code.value}:"
                    f"{0 if route.is_enabled else 1}"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                text="تعديل عنوان الدفع",
                callback_data=f"admin:addr:{route.code.value}",
            )
        ],
        [
            InlineKeyboardButton(
                text="تغيير صورة الباركود",
                callback_data=f"admin:photo:{route.code.value}",
            )
        ],
        [
            InlineKeyboardButton(
                text="العودة إلى قائمة المسارات",
                callback_data="admin:routes",
            )
        ],
    ])


def admin_request_list_menu(request_ids: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"عرض الطلب رقم {request_id}", callback_data=f"admin:view:{request_id}")]
            for request_id in request_ids]
    rows.append([InlineKeyboardButton(text="العودة إلى لوحة التحكم", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def review_menu(public_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="الموافقة وإتمام التحويل", callback_data=f"admin:ok:{public_id}")],
        [InlineKeyboardButton(text="رفض الطلب", callback_data=f"admin:no:{public_id}")],
        [InlineKeyboardButton(text="العودة إلى لوحة التحكم", callback_data="admin:home")],
    ])

def admin_payment_waiting_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="تحديث قائمة الطلبات",
                    callback_data="admin:pending",
                )
            ],
            [
                InlineKeyboardButton(
                    text="العودة إلى لوحة التحكم",
                    callback_data="admin:home",
                )
            ],
        ]
    )


def admin_withdrawal_menu(public_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="اعتماد ودفع", callback_data=f"admin:withdrawal:approve:{public_id}")],
        [InlineKeyboardButton(text="رفض طلب السحب", callback_data=f"admin:withdrawal:reject:{public_id}")],
        [InlineKeyboardButton(text="تحديث القائمة", callback_data="admin:withdrawals")],
        [InlineKeyboardButton(text="العودة إلى لوحة التحكم", callback_data="admin:home")],
    ])


def admin_withdrawal_list_menu(public_ids: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=f"تفاصيل #{public_id}", callback_data=f"admin:withdrawal:view:{public_id}"),
            InlineKeyboardButton(text=f"اعتماد #{public_id}", callback_data=f"admin:withdrawal:approve:{public_id}"),
            InlineKeyboardButton(text=f"رفض #{public_id}", callback_data=f"admin:withdrawal:reject:{public_id}"),
        ]
        for public_id in public_ids
    ]
    rows.extend([
        [InlineKeyboardButton(text="تحديث القائمة", callback_data="admin:withdrawals")],
        [InlineKeyboardButton(text="العودة إلى لوحة التحكم", callback_data="admin:home")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_referrals_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="تحديث قائمة الإحالات", callback_data="admin:referrals")],
        [InlineKeyboardButton(text="العودة إلى لوحة التحكم", callback_data="admin:home")],
    ])

def user_management_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="حظر مستخدم", callback_data="admin:ban:set")],
        [InlineKeyboardButton(text="رفع الحظر عن مستخدم", callback_data="admin:ban:unset")],
        [InlineKeyboardButton(text="العودة إلى لوحة التحكم", callback_data="admin:home")],
    ])


def broadcast_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="إرسال إلى جميع المستخدمين", callback_data="admin:broadcast:all")],
        [InlineKeyboardButton(text="إرسال رسالة إلى مستخدم واحد", callback_data="admin:broadcast:one")],
        [InlineKeyboardButton(text="العودة إلى لوحة التحكم", callback_data="admin:home")],
    ])


def monitoring_reply_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="الرد على هذا المستخدم", callback_data=f"admin:reply:{user_id}")]
    ])


def confirm_broadcast_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="تأكيد الإرسال الجماعي", callback_data="admin:broadcast:confirm")],
        [InlineKeyboardButton(text="إلغاء", callback_data="admin:broadcast:cancel")],
    ])
