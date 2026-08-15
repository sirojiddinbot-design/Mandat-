import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------
# SOZLAMALAR
# ---------------------------------------------------------
TOKEN = os.environ["TELEGRAM_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
SEED_CHANNELS = os.environ.get("FORCE_CHANNELS", "")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BASE_DIR = DATA_DIR
except Exception:
    BASE_DIR = Path(__file__).parent

SUBS_FILE = BASE_DIR / "subscribers.json"
CHANNELS_FILE = BASE_DIR / "channels.json"
PROGRAMS_FILE = BASE_DIR / "programs.json"
ADS_FILE = BASE_DIR / "ads.json"
ORDERS_FILE = BASE_DIR / "orders.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# Xavfsiz tahrirlash
# ---------------------------------------------------------
async def safe_edit_text(message, text, **kwargs):
    try:
        await message.edit_text(text, **kwargs)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def safe_edit_markup(message, reply_markup=None):
    try:
        await message.edit_reply_markup(reply_markup=reply_markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def global_error_handler(update, context) -> None:
    logger.error(f"Kutilmagan xato: {context.error}", exc_info=context.error)


# ---------------------------------------------------------
# Saqlash / o'qish
# ---------------------------------------------------------
def load_json_set(path: Path) -> set:
    if path.exists():
        return set(json.loads(path.read_text()))
    return set()


def save_json_set(path: Path, data: set) -> None:
    path.write_text(json.dumps(list(data)))


subscribers: set[int] = load_json_set(SUBS_FILE)

if CHANNELS_FILE.exists():
    force_channels: set[str] = load_json_set(CHANNELS_FILE)
else:
    force_channels = {c.strip() for c in SEED_CHANNELS.split(",") if c.strip()}
    save_json_set(CHANNELS_FILE, force_channels)


# Yo'nalishlar: {id: {"name": str, "ball": float, "fan": str}}
def load_programs() -> dict:
    if not PROGRAMS_FILE.exists():
        return {}
    raw = json.loads(PROGRAMS_FILE.read_text())
    fixed = {}
    for pid, val in raw.items():
        if isinstance(val, dict):
            fixed[pid] = {
                "name": val.get("name", ""),
                "ball": val.get("ball"),
                "fan": val.get("fan", ""),
            }
        else:
            fixed[pid] = {"name": val, "ball": None, "fan": ""}
    return fixed


def save_programs(data: dict) -> None:
    PROGRAMS_FILE.write_text(json.dumps(data, ensure_ascii=False))


programs: dict[str, dict] = load_programs()


# Reklamalar: {id: {"text": str, "active": bool, "views": int}}
# + sozlamalar: {"show_on_start": bool, "show_on_result": bool}
def load_ads() -> dict:
    if ADS_FILE.exists():
        data = json.loads(ADS_FILE.read_text())
        return {
            "items": data.get("items", {}),
            "show_on_start": data.get("show_on_start", True),
            "show_on_result": data.get("show_on_result", True),
        }
    return {"items": {}, "show_on_start": True, "show_on_result": True}


def save_ads(data: dict) -> None:
    ADS_FILE.write_text(json.dumps(data, ensure_ascii=False))


ads: dict = load_ads()


# ---------------------------------------------------------
# Buyurtmalar (abituriyent ID ro'yxati)
# Faqat ID raqami saqlanadi — hech qanday shaxsiy ma'lumot emas.
# {"counter": int, "items": {"<user_id>": [{"abit_id": str, "order_no": int}]}}
# ---------------------------------------------------------
def load_orders() -> dict:
    if ORDERS_FILE.exists():
        data = json.loads(ORDERS_FILE.read_text())
        return {"counter": data.get("counter", 0), "items": data.get("items", {})}
    return {"counter": 0, "items": {}}


def save_orders(data: dict) -> None:
    ORDERS_FILE.write_text(json.dumps(data, ensure_ascii=False))


orders: dict = load_orders()


def add_order(user_id: int, abit_id: str) -> tuple[int, bool]:
    """Buyurtma qo'shadi. Qaytaradi: (tartib raqami, yangi_mi)"""
    key = str(user_id)
    user_orders = orders["items"].setdefault(key, [])
    for o in user_orders:
        if o["abit_id"] == abit_id:
            return o["order_no"], False
    orders["counter"] += 1
    order_no = orders["counter"]
    user_orders.append({"abit_id": abit_id, "order_no": order_no})
    save_orders(orders)
    return order_no, True


def total_orders() -> int:
    return sum(len(v) for v in orders["items"].values())

admin_state: dict[int, str] = {}
pending_broadcast: dict[int, dict] = {}
user_calc_state: dict[int, dict] = {}


# ---------------------------------------------------------
# Reklama yordamchilari
# ---------------------------------------------------------
def get_active_ad() -> tuple[str, str] | None:
    """Faol reklamalardan birini qaytaradi: (ad_id, text)"""
    active = [(aid, a) for aid, a in ads["items"].items() if a.get("active")]
    if not active:
        return None
    # Eng kam ko'rilganini tanlaymiz (teng taqsimlash uchun)
    aid, ad = min(active, key=lambda x: x[1].get("views", 0))
    return aid, ad["text"]


def register_ad_view(ad_id: str) -> None:
    if ad_id in ads["items"]:
        ads["items"][ad_id]["views"] = ads["items"][ad_id].get("views", 0) + 1
        save_ads(ads)


async def send_ad(bot, chat_id: int, place: str = "all") -> None:
    """Reklamani ALOHIDA xabar sifatida yuboradi.
    Har bir bo'limdan keyin chaqiriladi (bosh menyu, kalkulyator natijasi,
    superkontrakt, buyurtma, yordam)."""
    if place == "start" and not ads.get("show_on_start", True):
        return
    if place == "result" and not ads.get("show_on_result", True):
        return

    result = get_active_ad()
    if not result:
        return
    ad_id, ad_text = result
    register_ad_view(ad_id)

    try:
        await bot.send_message(
            chat_id,
            ad_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
    except BadRequest:
        # HTML buzuq bo'lsa, oddiy matn sifatida yuboramiz — reklama
        # baribir yetib borsin
        try:
            await bot.send_message(chat_id, ad_text, disable_web_page_preview=False)
        except Exception as e:
            logger.warning(f"Reklama yuborilmadi {chat_id}: {e}")
    except Exception as e:
        # Reklama yuborilmasa ham asosiy ish buzilmasligi kerak
        logger.warning(f"Reklama yuborilmadi {chat_id}: {e}")


# ---------------------------------------------------------
# Majburiy obuna (kesh bilan — tezkor ishlashi uchun)
# ---------------------------------------------------------
# Foydalanuvchi a'zoligi natijasini vaqtincha eslab qolamiz,
# shunda har tugma bosilganda Telegram'ga qayta so'rov ketmaydi.
_membership_cache: dict[int, tuple[float, list[str]]] = {}
MEMBERSHIP_CACHE_SECONDS = 1800  # 30 daqiqa — so'rovlarni kamaytirish uchun
MEMBERSHIP_CACHE_MAX = 20000  # Keshning xotirada cheksiz o'smasligi uchun


async def get_missing_channels(bot, user_id: int, use_cache: bool = True) -> list[str]:
    if not force_channels:
        return []

    now = time.time()
    if use_cache:
        cached = _membership_cache.get(user_id)
        if cached and now - cached[0] < MEMBERSHIP_CACHE_SECONDS:
            return cached[1]

    # Barcha kanallarni parallel tekshiramiz (ketma-ket emas — ancha tez)
    async def check(channel: str) -> str | None:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            return channel if member.status in ("left", "kicked") else None
        except Forbidden:
            # Bot kanalda admin emas — tekshira olmaymiz, foydalanuvchini
            # bekorga to'sib qo'ymaymiz
            logger.warning(f"{channel}: bot admin emas")
            return None
        except Exception as e:
            # Tarmoq xatosi bo'lsa ham foydalanuvchini to'smaymiz —
            # aks holda bot ishlamay qolgandek tuyuladi
            logger.warning(f"{channel} tekshirilmadi: {e}")
            return None

    try:
        results = await asyncio.gather(*(check(c) for c in force_channels))
    except Exception as e:
        logger.warning(f"Obuna tekshiruvi xatosi: {e}")
        return []

    missing = [c for c in results if c]

    # Kesh haddan tashqari o'sib ketmasligi uchun eskilarini tozalaymiz
    if len(_membership_cache) > MEMBERSHIP_CACHE_MAX:
        eski = [k for k, v in _membership_cache.items()
                if now - v[0] > MEMBERSHIP_CACHE_SECONDS]
        for k in eski:
            _membership_cache.pop(k, None)

    _membership_cache[user_id] = (now, missing)
    return missing


def build_subscription_keyboard(missing: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for channel in missing:
        username = channel.lstrip("@")
        rows.append([InlineKeyboardButton(f"📢  {channel}", url=f"https://t.me/{username}")])
    rows.append([InlineKeyboardButton("✅  Tekshirdim", callback_data="check_sub")])
    return InlineKeyboardMarkup(rows)


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Pastdagi doimiy menyu — har doim ko'rinib turadi."""
    return ReplyKeyboardMarkup(
        [
            ["🎯 Mandat natijasini ko'rish"],
            ["📝 Mandatga buyurtma"],
            ["🧮 Ball kalkulyatori"],
            ["💰 Superkontrakt", "🔔 Eslatma"],
            ["ℹ️ Yordam"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# ---------------------------------------------------------
# Mandat natijasini ko'rish (rasmiy saytlarga yo'naltirish)
# ---------------------------------------------------------
NATIJA_TEXT = (
    "🎯 <b>MANDAT NATIJASINI KO'RISH</b>\n"
    "━━━━━━━━━━━━━━━\n\n"
    "Natijangizni rasmiy saytlarning birida ko'rishingiz mumkin:\n\n"
    "1️⃣ <b>my.uzbmb.uz</b>\n"
    "<i>Pasport seriyasi va raqami + JSHSHIR orqali</i>\n\n"
    "2️⃣ <b>mandat.uzbmb.uz</b>\n"
    "<i>Abituriyent ID raqami orqali</i>\n\n"
    "━━━━━━━━━━━━━━━\n"
    "👇 Kerakli saytni tanlang"
)


def natija_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("1️⃣  my.uzbmb.uz  🔗", url="https://my.uzbmb.uz")],
            [InlineKeyboardButton("2️⃣  mandat.uzbmb.uz  🔗", url="https://mandat.uzbmb.uz")],
            [InlineKeyboardButton("◀️  Bosh menyu", callback_data="calc_cancel")],
        ]
    )


async def show_main_menu(chat_id: int, bot, edit_message=None, user_name: str = "",
                         with_keyboard: bool = False) -> None:
    is_subscribed = chat_id in subscribers
    button_text = "🔕 Eslatmani o'chirish" if is_subscribed else "🔔 Eslatmani yoqish"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎯  Mandat natijasini ko'rish", callback_data="show_natija")],
            [InlineKeyboardButton("📝  Yakuniy mandatga buyurtma", callback_data="start_order")],
            [InlineKeyboardButton("🧮  Ball kalkulyatori", callback_data="start_calc")],
            [InlineKeyboardButton("💰  Superkontrakt hisoblash", callback_data="start_super")],
            [InlineKeyboardButton(button_text, callback_data="toggle_sub")],
        ]
    )

    salom = f"Assalomu alaykum, {user_name}! 👋\n\n" if user_name else "Assalomu alaykum! 👋\n\n"
    holat = "✅ Yoqilgan" if is_subscribed else "⚪️ O'chirilgan"

    text = (
        f"{salom}"
        "🎓 <b>MANDAT 2026</b>\n"
        "<i>Abituriyentlar uchun yordamchi</i>\n"
        "━━━━━━━━━━━━━━━\n\n"
        "🎯 <b>Mandat natijasini ko'rish</b>\n"
        "<i>Rasmiy saytlarga to'g'ridan-to'g'ri o'tish</i>\n\n"
        "📝 <b>Yakuniy mandatga buyurtma</b>\n"
        "<i>ID raqamingizni qoldiring — natija chiqishi bilan xabar olasiz</i>\n\n"
        "🧮 <b>Ball kalkulyatori</b>\n"
        "<i>Ballingizga mos yo'nalishlarni bir zumda aniqlang</i>\n\n"
        "💰 <b>Superkontrakt hisoblash</b>\n"
        "<i>Ball yetmasa — to'lov necha barobar bo'lishini biling</i>\n\n"
        "🔔 <b>Natija eslatmasi</b>\n"
        "<i>Mandat e'lon qilinishi bilan xabar olasiz</i>\n"
        f"Holat: {holat}\n\n"
        "━━━━━━━━━━━━━━━\n"
        "👇 Quyidagi tugmalardan birini tanlang"
    )

    if edit_message:
        await safe_edit_text(edit_message, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        # Klaviatura kerak bo'lsa, avval uni menyu matni bilan birga o'rnatamiz.
        # Inline tugmalar va pastki klaviatura bitta xabarda bo'la olmaydi,
        # shuning uchun klaviatura menyudan oldin, qisqa xabar bilan ketadi.
        if with_keyboard:
            await bot.send_message(
                chat_id,
                "⌨️ Pastdagi tugmalar orqali tez kirishingiz mumkin 👇",
                reply_markup=main_reply_keyboard(),
            )
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    # Reklama alohida xabar sifatida
    await send_ad(bot, chat_id, "start")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name or ""

    if force_channels:
        missing = await get_missing_channels(context.bot, chat_id)
        if missing:
            await update.message.reply_text(
                "🔐 <b>Bir qadam qoldi!</b>\n"
                "━━━━━━━━━━━━━━━\n\n"
                "Botdan bepul foydalanish uchun quyidagi kanal(lar)ga a'zo bo'ling:\n\n"
                "<i>A'zo bo'lgach «✅ Tekshirdim» tugmasini bosing</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=build_subscription_keyboard(missing),
            )
            return

    # Klaviatura menyu xabarining o'zi bilan birga o'rnatiladi —
    # ortiqcha xabar yuborilmaydi (bu Telegram'ga so'rovni kamaytiradi)
    await show_main_menu(chat_id, context.bot, user_name=user_name, with_keyboard=True)


async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    # "Tekshirdim" bosilganda keshni ishlatmaymiz — foydalanuvchi hozirgina
    # obuna bo'lgan bo'lishi mumkin, shuning uchun yangidan tekshiramiz.
    missing = await get_missing_channels(context.bot, chat_id, use_cache=False)
    if missing:
        await query.answer("❌ Hali barcha kanallarga a'zo bo'lmadingiz", show_alert=True)
        await safe_edit_markup(query.message, reply_markup=build_subscription_keyboard(missing))
        return
    await query.answer("✅ Rahmat! Xush kelibsiz")
    await show_main_menu(chat_id, context.bot,
                         user_name=query.from_user.first_name or "",
                         with_keyboard=True)


async def toggle_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    if force_channels:
        missing = await get_missing_channels(context.bot, chat_id)
        if missing:
            await query.answer("❌ Avval kanallarga a'zo bo'ling", show_alert=True)
            await safe_edit_markup(query.message, reply_markup=build_subscription_keyboard(missing))
            return
    if chat_id in subscribers:
        subscribers.remove(chat_id)
        save_json_set(SUBS_FILE, subscribers)
        await query.answer("🔕 Eslatma o'chirildi", show_alert=True)
    else:
        subscribers.add(chat_id)
        save_json_set(SUBS_FILE, subscribers)
        await query.answer(
            "🔔 Eslatma yoqildi!\n\nNatijalar e'lon qilinishi bilan sizga xabar yuboriladi.",
            show_alert=True,
        )
    await show_main_menu(chat_id, context.bot, edit_message=query.message,
                         user_name=query.from_user.first_name or "")


# ===========================================================
# BALL KALKULYATORI (fan majmuasi bilan)
# ===========================================================
def get_all_fans() -> list[str]:
    fans = {p["fan"] for p in programs.values() if p.get("fan") and p.get("ball") is not None}
    return sorted(fans)


def build_fan_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"📚  {f}", callback_data=f"calcfan:{f}")] for f in get_all_fans()]
    rows.append([InlineKeyboardButton("◀️  Bosh menyu", callback_data="calc_cancel")])
    return InlineKeyboardMarkup(rows)


async def start_calc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    fans = get_all_fans()
    if not fans:
        await query.answer()
        await query.message.reply_text(
            "⏳ Yo'nalishlar bazasi hozircha to'ldirilmoqda.\nBirozdan so'ng qayta urinib ko'ring."
        )
        return
    await query.answer()
    await query.message.reply_text(
        "🧮 <b>BALL KALKULYATORI</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        "<b>1-qadam:</b> Fanlar majmuangizni tanlang 👇",
        parse_mode=ParseMode.HTML,
        reply_markup=build_fan_keyboard(),
    )


async def calc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    action = query.data

    if action == "calc_cancel":
        user_calc_state.pop(chat_id, None)
        user_super_state.pop(chat_id, None)
        user_order_state.pop(chat_id, None)
        await query.answer()
        await safe_edit_text(query.message, "◀️ Bosh menyuga qaytdingiz.")
        await show_main_menu(chat_id, context.bot)
        return

    if action.startswith("calcfan:"):
        fan = action.split(":", 1)[1]
        user_calc_state[chat_id] = {"stage": "awaiting_score", "fan": fan}
        await query.answer()
        await safe_edit_text(
            query.message,
            "🧮 <b>BALL KALKULYATORI</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            f"📚 Fanlar: <b>{fan}</b>\n\n"
            "<b>2-qadam:</b> To'plagan umumiy ballingizni yozing\n\n"
            "<i>Masalan: 165.3</i>",
            parse_mode=ParseMode.HTML,
        )
        return


def superkontrakt_koeffitsiyent(farq: float) -> tuple[float | None, str]:
    """Ball farqiga qarab superkontrakt koeffitsiyentini qaytaradi.
    farq — o'tish balliga qancha ball yetmagani (musbat son)."""
    if farq <= 0:
        return None, ""
    if farq <= 1.05:
        return 1.5, "1,05 ballgacha yetmaganlar"
    if farq <= 2.05:
        return 2.0, "1,06–2,05 ball yetmaganlar"
    if farq <= 3.05:
        return 2.5, "2,06–3,05 ball yetmaganlar"
    if farq <= 4.05:
        return 3.0, "3,06–4,05 ball yetmaganlar"
    return None, "4,05 balldan ortiq"


def format_super_result(score: float, fan: str) -> str:
    """Ballga yetmagan, lekin superkontrakt imkoniyati bor yo'nalishlar."""
    kandidatlar = []
    for p in programs.values():
        if p.get("ball") is None or p.get("fan") != fan:
            continue
        farq = round(p["ball"] - score, 2)
        if 0 < farq <= 4.05:
            koef, _ = superkontrakt_koeffitsiyent(farq)
            if koef:
                kandidatlar.append((p, farq, koef))

    if not kandidatlar:
        return ""

    kandidatlar.sort(key=lambda x: x[1])
    lines = [
        "\n\n💰 <b>SUPERKONTRAKT IMKONIYATI</b>",
        "━━━━━━━━━━━━━━━",
        "<i>Quyidagi yo'nalishlarga ballingiz sal yetmadi, lekin "
        "superkontrakt asosida o'qish imkoniyati bor:</i>\n",
    ]
    for p, farq, koef in kandidatlar[:10]:
        koef_str = str(koef).replace(".", ",")
        lines.append(
            f"🔸 <b>{p['name']}</b>\n"
            f"     └ O'tish balli: {p['ball']}  ·  <b>{farq}</b> ball yetmadi\n"
            f"     └ To'lov: bazaviy kontraktning <b>{koef_str} barobari</b>"
        )
    lines.append(
        "\n📐 <i>Aniq summani bilish uchun «💰 Superkontrakt hisoblash» "
        "tugmasidan foydalaning.</i>"
    )
    return "\n".join(lines)


def format_calc_result(score: float, fan: str) -> str:
    matches = [
        p for p in programs.values()
        if p.get("ball") is not None and p.get("fan") == fan and p["ball"] <= score
    ]
    matches.sort(key=lambda p: p["ball"], reverse=True)

    all_in_fan = [p for p in programs.values() if p.get("fan") == fan and p.get("ball") is not None]

    header = (
        "📊 <b>NATIJA</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"🎯 Sizning ballingiz: <b>{score}</b>\n"
        f"📚 Fanlar: <b>{fan}</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
    )

    if not matches:
        min_ball = min((p["ball"] for p in all_in_fan), default=None)
        body = header + "😔 Afsuski, bu fanlar bo'yicha ballingizga mos yo'nalish topilmadi."
        if min_ball is not None:
            farq = round(min_ball - score, 1)
            body += (
                f"\n\n📉 Eng past o'tish balli: <b>{min_ball}</b>\n"
                f"Sizga yana <b>{farq}</b> ball yetishmayapti."
            )
        body += "\n\n💡 Boshqa fanlar majmuasini ham tekshirib ko'ring."
    else:
        lines = [header, f"✅ Sizga mos <b>{len(matches)} ta</b> yo'nalish topildi:\n"]
        for i, p in enumerate(matches[:25], 1):
            farq = round(score - p["ball"], 1)
            if farq >= 20:
                belgi = "🟢"
            elif farq >= 5:
                belgi = "🟡"
            else:
                belgi = "🟠"
            lines.append(
                f"{belgi} <b>{i}. {p['name']}</b>\n"
                f"     └ O'tish balli: {p['ball']}  ·  sizda <b>+{farq}</b>"
            )
        if len(matches) > 25:
            lines.append(f"\n<i>... va yana {len(matches) - 25} ta yo'nalish</i>")
        lines.append(
            "\n🟢 Ishonchli  ·  🟡 O'rtacha  ·  🟠 Chegaraga yaqin"
        )
        body = "\n".join(lines)

    # Superkontrakt imkoniyati bo'lgan yo'nalishlarni ham qo'shamiz
    body += format_super_result(score, fan)

    body += (
        "\n\n━━━━━━━━━━━━━━━\n"
        "📌 <i>2025/2026 ko'rsatkichlari asosida taxminiy hisob. "
        "Rasmiy ma'lumot: mandat.uzbmb.uz</i>"
    )
    return body


# ===========================================================
# SUPERKONTRAKT HISOBLAGICHI
# ===========================================================
user_super_state: dict[int, dict] = {}


async def start_super(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    user_super_state[chat_id] = {"stage": "awaiting_farq"}
    await query.answer()
    await query.message.reply_text(
        "💰 <b>SUPERKONTRAKT HISOBLAGICHI</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        "<b>1-qadam:</b> O'tish balliga necha ball yetmaganini yozing\n\n"
        "<i>Masalan: 2.4</i>\n\n"
        "<i>(Ya'ni: o'tish balli 130, sizda 127.6 bo'lsa → 2.4 deb yozing)</i>",
        parse_mode=ParseMode.HTML,
    )


def format_super_calc(farq: float, bazaviy: float | None = None) -> str:
    koef, izoh = superkontrakt_koeffitsiyent(farq)

    if farq <= 0:
        return (
            "✅ Ballingiz yetarli ekan — superkontrakt kerak emas!\n\n"
            "Oddiy kontrakt asosida o'qishingiz mumkin."
        )

    if koef is None:
        return (
            "💰 <b>SUPERKONTRAKT</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            f"📉 Yetmagan ball: <b>{farq}</b>\n\n"
            "⚠️ 4,05 balldan ortiq yetmagan bo'lsangiz, superkontrakt miqdorini "
            "OTM rektori mustaqil belgilaydi.\n\n"
            "Qonun bo'yicha u bazaviy kontraktning <b>3 barobaridan kam</b> "
            "bo'lmasligi kerak, lekin aniq miqdorni faqat OTM qabul "
            "komissiyasidan bilib olishingiz mumkin.\n\n"
            "📞 <i>OTM qabul komissiyasiga murojaat qiling.</i>"
        )

    koef_str = str(koef).replace(".", ",")
    body = (
        "💰 <b>SUPERKONTRAKT</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"📉 Yetmagan ball: <b>{farq}</b>\n"
        f"📋 Toifa: {izoh}\n"
        f"📐 Koeffitsiyent: <b>{koef_str} barobar</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
    )

    if bazaviy:
        summa = bazaviy * koef
        body += (
            f"💵 Bazaviy kontrakt: <b>{bazaviy:,.0f}</b> so'm\n"
            f"💰 Superkontrakt: <b>{summa:,.0f}</b> so'm\n\n"
        ).replace(",", " ")
    else:
        body += (
            "💵 Aniq summani bilish uchun yo'nalishingizning "
            "<b>bazaviy kontrakt narxini</b> yozing (so'mda).\n\n"
            "<i>Masalan: 15000000</i>\n\n"
            "<i>Bazaviy narxni OTM saytidan yoki qabul komissiyasidan bilib olasiz.</i>"
        )

    body += (
        "\n━━━━━━━━━━━━━━━\n"
        "📌 <i>Bu — qonundagi minimal koeffitsiyent asosida hisob. "
        "OTM'lar narxni mustaqil belgilashi mumkin, shuning uchun aniq summani "
        "qabul komissiyasidan tasdiqlang.</i>"
    )
    return body


# ===========================================================
# YAKUNIY MANDATGA BUYURTMA (faqat ID saqlanadi)
# ===========================================================
user_order_state: dict[int, bool] = {}

ORDER_PROMPT = (
    "📝 <b>YAKUNIY MANDATGA BUYURTMA</b>\n"
    "━━━━━━━━━━━━━━━\n\n"
    "Mandat natijalari hali e'lon qilinmagan.\n\n"
    "Abituriyent <b>ID raqamingizni</b> yuboring — natijalar e'lon qilinishi "
    "bilan bot sizga darhol xabar beradi va natijani ko'rish havolasini yuboradi.\n\n"
    "🆔 <i>ID raqami qayd varaqangizning yuqorisida yozilgan (7 xonali son)</i>\n\n"
    "<i>Masalan: 1234567</i>\n\n"
    "📌 <i>Bot faqat ID raqamini saqlaydi — boshqa hech qanday shaxsiy "
    "ma'lumot so'ralmaydi va saqlanmaydi.</i>"
)


async def start_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    user_order_state[chat_id] = True
    await query.answer()
    await query.message.reply_text(ORDER_PROMPT, parse_mode=ParseMode.HTML)


async def show_natija(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mandat natijasini ko'rish — rasmiy saytlarga havolalar."""
    query = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()
    await query.message.reply_text(
        NATIJA_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=natija_keyboard(),
        disable_web_page_preview=True,
    )
    await send_ad(context.bot, chat_id, "result")


async def count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(f"Hozirgi obunachilar soni: {len(subscribers)}")


# ===========================================================
# ADMIN PANEL
# ===========================================================
def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Xabar yuborish", callback_data="admin_broadcast")],
            [
                InlineKeyboardButton("➕ Kanal qo'shish", callback_data="admin_add_channel"),
                InlineKeyboardButton("➖ Kanal o'chirish", callback_data="admin_remove_channel"),
            ],
            [
                InlineKeyboardButton("📋 Kanallar", callback_data="admin_list_channels"),
                InlineKeyboardButton("📊 Statistika", callback_data="admin_stats"),
            ],
            [InlineKeyboardButton("📥 Ro'yxatni tiklash (import)", callback_data="admin_import")],
            [InlineKeyboardButton("🎓 Yo'nalishlar bazasi", callback_data="admin_programs")],
            [InlineKeyboardButton("📣 Reklama bo'limi", callback_data="admin_ads")],
            [InlineKeyboardButton("❌ Yopish", callback_data="admin_close")],
        ]
    )


def ads_menu_keyboard() -> InlineKeyboardMarkup:
    start_status = "✅ Yoniq" if ads.get("show_on_start") else "❌ O'chiq"
    result_status = "✅ Yoniq" if ads.get("show_on_result") else "❌ O'chiq"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"🚀 Start: {start_status}", callback_data="ad_toggle_start"),
                InlineKeyboardButton(f"🧮 Natija: {result_status}", callback_data="ad_toggle_result"),
            ],
            [InlineKeyboardButton("➕ Reklama qo'shish", callback_data="ad_add")],
            [InlineKeyboardButton("📋 Reklamalar ro'yxati", callback_data="ad_list")],
            [InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_back")],
        ]
    )


def ads_summary_text() -> str:
    total = len(ads["items"])
    active = sum(1 for a in ads["items"].values() if a.get("active"))
    views = sum(a.get("views", 0) for a in ads["items"].values())
    return (
        "📣 <b>REKLAMA BO'LIMI</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"📊 Jami reklamalar: <b>{total}</b> ta\n"
        f"✅ Faol: <b>{active}</b> ta\n"
        f"👁 Jami ko'rishlar: <b>{views}</b> ta\n\n"
        "⚙️ <b>Sozlamalar:</b>\n"
        f"🚀 Bosh menyuda: {'✅ Yoniq' if ads.get('show_on_start') else '❌ O`chiq'}\n"
        f"🧮 Bo'limlardan keyin: {'✅ Yoniq' if ads.get('show_on_result') else '❌ O`chiq'}\n\n"
        "━━━━━━━━━━━━━━━\n"
        "📌 <i>Reklama alohida xabar sifatida yuboriladi — bosh menyu, "
        "natija ko'rish, kalkulyator, superkontrakt, buyurtma va yordam "
        "bo'limlaridan keyin.</i>\n\n"
        "✨ <i>Matnda HTML formatlash ishlatishingiz mumkin: "
        "&lt;b&gt;qalin&lt;/b&gt;, &lt;i&gt;qiya&lt;/i&gt;, "
        "&lt;a href=\"havola\"&gt;matn&lt;/a&gt;</i>"
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    # Barcha kutilayotgan holatlarni tozalaymiz — shunda admin panelga
    # kirganda oldingi tugallanmagan amallar xalaqit bermaydi
    admin_state.pop(ADMIN_ID, None)
    pending_broadcast.pop(ADMIN_ID, None)
    user_calc_state.pop(ADMIN_ID, None)
    user_super_state.pop(ADMIN_ID, None)
    user_order_state.pop(ADMIN_ID, None)
    await update.message.reply_text(
        "🛠 <b>Admin panel</b>\n\nKerakli bo'limni tanlang:",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu_keyboard(),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Ruxsat yo'q", show_alert=True)
        return

    action = query.data

    if action == "admin_close":
        admin_state.pop(ADMIN_ID, None)
        await query.answer()
        await safe_edit_text(query.message, "Panel yopildi. Qayta ochish uchun /admin yozing.")
        return

    if action == "admin_stats":
        await query.answer()
        scored = sum(1 for p in programs.values() if p.get("ball") is not None)
        await safe_edit_text(
            query.message,
            f"📊 <b>Statistika</b>\n\n"
            f"Obunachilar: {len(subscribers)}\n"
            f"Mandat buyurtmalari: {total_orders()} ta\n"
            f"Majburiy kanallar: {len(force_channels)}\n"
            f"Yo'nalishlar: {len(programs)} (balli: {scored})\n"
            f"Fan majmualari: {len(get_all_fans())}",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard(),
        )
        return

    if action == "admin_list_channels":
        await query.answer()
        if force_channels:
            text = "📋 <b>Majburiy kanallar:</b>\n\n" + "\n".join(f"• {c}" for c in force_channels)
        else:
            text = "📋 Hozircha majburiy kanal yo'q."
        await safe_edit_text(query.message, text, parse_mode=ParseMode.HTML, reply_markup=admin_menu_keyboard())
        return

    if action == "admin_import":
        admin_state[ADMIN_ID] = "awaiting_import"
        await query.answer()
        await safe_edit_text(
            query.message,
            "📥 Obunachilar ID ro'yxatini yuboring (raqamlar, istalgan formatda).\n\n"
            "Bekor qilish uchun /admin yozing.",
        )
        return

    if action == "admin_add_channel":
        admin_state[ADMIN_ID] = "awaiting_add_channel"
        await query.answer()
        await safe_edit_text(
            query.message,
            "➕ Yangi kanal username'ini yuboring (masalan: @mening_kanalim).\n\n"
            "❗️ Bot o'sha kanalga <b>admin</b> qilib qo'shilgan bo'lishi kerak.\n"
            "Bekor qilish uchun /admin yozing.",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "admin_remove_channel":
        await query.answer()
        if not force_channels:
            await safe_edit_text(query.message, "Hozircha kanal yo'q.", reply_markup=admin_menu_keyboard())
            return
        rows = [[InlineKeyboardButton(f"🗑 {c}", callback_data=f"delch:{c}")] for c in force_channels]
        rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_back")])
        await safe_edit_text(query.message, "O'chirmoqchi bo'lgan kanalni tanlang:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == "admin_back":
        admin_state.pop(ADMIN_ID, None)
        await query.answer()
        await safe_edit_text(query.message, "🛠 <b>Admin panel</b>", parse_mode=ParseMode.HTML, reply_markup=admin_menu_keyboard())
        return

    if action.startswith("delch:"):
        channel = action.split(":", 1)[1]
        force_channels.discard(channel)
        save_json_set(CHANNELS_FILE, force_channels)
        await query.answer(f"{channel} o'chirildi ✅")
        await safe_edit_text(query.message, f"✅ {channel} o'chirildi.", reply_markup=admin_menu_keyboard())
        return

    # ---------- Yo'nalishlar ----------
    if action == "admin_programs":
        await query.answer()
        scored = sum(1 for p in programs.values() if p.get("ball") is not None)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Yo'nalish qo'shish", callback_data="admin_add_program")],
                [InlineKeyboardButton("🗑 Hammasini o'chirish", callback_data="admin_clear_programs")],
                [InlineKeyboardButton("📋 Ro'yxat", callback_data="admin_list_programs")],
                [InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_back")],
            ]
        )
        await safe_edit_text(
            query.message,
            f"🎓 <b>Yo'nalishlar bazasi</b>\n\n"
            f"Jami: {len(programs)} ta\nBalli kiritilgan: {scored} ta\n"
            f"Fan majmualari: {len(get_all_fans())} ta",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return

    if action == "admin_add_program":
        admin_state[ADMIN_ID] = "awaiting_add_program"
        await query.answer()
        await safe_edit_text(
            query.message,
            "➕ Yo'nalishlarni quyidagi formatda yuboring:\n\n"
            "<code>Nomi | Ball | Fanlar</code>\n\n"
            "Masalan:\n"
            "<code>Davolash ishi | 130.0 | Biologiya, Kimyo\n"
            "Dasturiy injiniring | 56.7 | Matematika, Fizika</code>\n\n"
            "Har birini yangi qatordan yozing.\n"
            "Bekor qilish uchun /admin yozing.",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "admin_clear_programs":
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Ha, o'chirilsin", callback_data="admin_clear_confirm")],
                [InlineKeyboardButton("❌ Yo'q", callback_data="admin_programs")],
            ]
        )
        await query.answer()
        await safe_edit_text(
            query.message,
            f"⚠️ Barcha {len(programs)} ta yo'nalish o'chiriladi. Tasdiqlaysizmi?",
            reply_markup=kb,
        )
        return

    if action == "admin_clear_confirm":
        programs.clear()
        save_programs(programs)
        await query.answer("O'chirildi ✅")
        await safe_edit_text(query.message, "✅ Barcha yo'nalishlar o'chirildi.", reply_markup=admin_menu_keyboard())
        return

    if action == "admin_list_programs":
        await query.answer()
        if programs:
            lines = []
            for p in list(programs.values())[:60]:
                ball = p["ball"] if p.get("ball") is not None else "—"
                lines.append(f"• {p['name']} | {ball} | {p.get('fan', '—')}")
            text = "📋 <b>Yo'nalishlar:</b>\n\n" + "\n".join(lines)
            if len(programs) > 60:
                text += f"\n\n... va yana {len(programs) - 60} ta"
        else:
            text = "📋 Hozircha yo'nalish yo'q."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_programs")]])
        await safe_edit_text(query.message, text[:4000], parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    # ---------- Reklama bo'limi ----------
    if action == "admin_ads":
        await query.answer()
        await safe_edit_text(query.message, ads_summary_text(), parse_mode=ParseMode.HTML, reply_markup=ads_menu_keyboard())
        return

    if action == "ad_toggle_start":
        ads["show_on_start"] = not ads.get("show_on_start")
        save_ads(ads)
        await query.answer("O'zgartirildi ✅")
        await safe_edit_text(query.message, ads_summary_text(), parse_mode=ParseMode.HTML, reply_markup=ads_menu_keyboard())
        return

    if action == "ad_toggle_result":
        ads["show_on_result"] = not ads.get("show_on_result")
        save_ads(ads)
        await query.answer("O'zgartirildi ✅")
        await safe_edit_text(query.message, ads_summary_text(), parse_mode=ParseMode.HTML, reply_markup=ads_menu_keyboard())
        return

    if action == "ad_add":
        admin_state[ADMIN_ID] = "awaiting_ad_text"
        await query.answer()
        await safe_edit_text(
            query.message,
            "➕ <b>YANGI REKLAMA</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            "Reklama matnini shu yerga yuboring.\n\n"
            "✨ <b>Formatlash:</b> matnni belgilab, Telegram'ning o'z formatlash "
            "menyusidan foydalaning (qalin, qiya, havola). Teglarni qo'lda "
            "yozish shart emas.\n\n"
            "🔗 Havola qo'shsangiz, oldindan ko'rish (preview) bilan chiroyli chiqadi.\n\n"
            "☝️ Yuborganingizdan so'ng qanday ko'rinishini darhol ko'rasiz.\n\n"
            "❌ Bekor qilish uchun /admin yozing.",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "ad_list":
        await query.answer()
        if not ads["items"]:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_ads")]])
            await safe_edit_text(query.message, "Hozircha reklama yo'q.", reply_markup=kb)
            return
        rows = []
        for aid, ad in ads["items"].items():
            status = "✅" if ad.get("active") else "❌"
            preview = ad["text"][:25].replace("\n", " ")
            rows.append([InlineKeyboardButton(f"{status} {preview}… ({ad.get('views', 0)})", callback_data=f"ad_view:{aid}")])
        rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_ads")])
        await safe_edit_text(query.message, "📋 Reklamalar (bosib boshqaring):", reply_markup=InlineKeyboardMarkup(rows))
        return

    if action.startswith("ad_view:"):
        aid = action.split(":", 1)[1]
        ad = ads["items"].get(aid)
        if not ad:
            await query.answer("Topilmadi")
            return
        await query.answer()
        status = "✅ Faol" if ad.get("active") else "❌ O'chiq"
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 Yoqish/O'chirish", callback_data=f"ad_toggle:{aid}")],
                [InlineKeyboardButton("🗑 O'chirish", callback_data=f"ad_del:{aid}")],
                [InlineKeyboardButton("⬅️ Orqaga", callback_data="ad_list")],
            ]
        )
        await safe_edit_text(
            query.message,
            f"📣 <b>Reklama</b>\n\nHolat: {status}\n👁 Ko'rishlar: {ad.get('views', 0)}\n\n{ad['text']}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return

    if action.startswith("ad_toggle:"):
        aid = action.split(":", 1)[1]
        if aid in ads["items"]:
            ads["items"][aid]["active"] = not ads["items"][aid].get("active")
            save_ads(ads)
            await query.answer("O'zgartirildi ✅")
        await safe_edit_text(query.message, ads_summary_text(), parse_mode=ParseMode.HTML, reply_markup=ads_menu_keyboard())
        return

    if action.startswith("ad_del:"):
        aid = action.split(":", 1)[1]
        ads["items"].pop(aid, None)
        save_ads(ads)
        await query.answer("O'chirildi ✅")
        await safe_edit_text(query.message, ads_summary_text(), parse_mode=ParseMode.HTML, reply_markup=ads_menu_keyboard())
        return

    # ---------- Broadcast ----------
    if action == "admin_broadcast":
        admin_state[ADMIN_ID] = "awaiting_broadcast"
        await query.answer()
        await safe_edit_text(
            query.message,
            "📢 <b>XABAR YUBORISH</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            f"👥 Qabul qiluvchilar: <b>{len(subscribers)}</b> kishi\n\n"
            "Yubormoqchi bo'lgan xabaringizni shu yerga yuboring:\n\n"
            "📝 Matn  ·  🖼 Rasm  ·  🎬 Video\n"
            "🎞 GIF  ·  📎 Fayl  ·  🎤 Ovozli xabar\n\n"
            "✨ <i>Formatlash (qalin matn, havolalar, emoji) va izohlar "
            "to'liq saqlanadi — xabar aynan siz yozgandek yetib boradi.</i>\n\n"
            "❌ Bekor qilish uchun /admin yozing.",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "bc_add_button":
        if not pending_broadcast.get(ADMIN_ID):
            await query.answer("⚠️ Xabar topilmadi", show_alert=True)
            return
        admin_state[ADMIN_ID] = "awaiting_bc_buttons"
        await query.answer()
        await safe_edit_text(
            query.message,
            "🔘 <b>TUGMA QO'SHISH</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            "Har bir tugmani <b>yangi qatorga</b> shunday yozing:\n\n"
            "<code>Tugma matni - havola</code>\n\n"
            "<b>Masalan:</b>\n"
            "<code>🎯 Natijani ko'rish - https://mandat.uzbmb.uz\n"
            "🤖 Botga o'tish - @MANDAT_AIBOT\n"
            "📢 Kanalimiz - https://t.me/kanal</code>\n\n"
            "━━━━━━━━━━━━━━━\n"
            "📌 Ko'pi bilan 10 ta tugma.\n"
            "❌ Bekor qilish uchun /admin yozing.",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "bc_clear_buttons":
        data = pending_broadcast.get(ADMIN_ID)
        if data:
            data.pop("buttons", None)
            pending_broadcast[ADMIN_ID] = data
        await query.answer("🗑 Tugmalar olib tashlandi")
        confirm_kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔘  Tugma qo'shish", callback_data="bc_add_button")],
                [InlineKeyboardButton("✅  Ha, yuborilsin", callback_data="confirm_broadcast")],
                [InlineKeyboardButton("❌  Bekor qilish", callback_data="cancel_broadcast")],
            ]
        )
        await safe_edit_text(
            query.message,
            "📢 <b>XABARNI TASDIQLASH</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            f"👥 Qabul qiluvchilar: <b>{len(subscribers)}</b> kishi\n"
            "🔘 Tugmalar: <b>yo'q</b>\n"
            f"⏱ Taxminiy vaqt: <b>~{max(1, round(len(subscribers) / 20 / 60))} daqiqa</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            "Yuborishni tasdiqlaysizmi?",
            parse_mode=ParseMode.HTML,
            reply_markup=confirm_kb,
        )
        return

    if action == "confirm_broadcast":
        await query.answer()
        data = pending_broadcast.pop(ADMIN_ID, None)
        if not data:
            await safe_edit_text(query.message, "⚠️ Xabar topilmadi. Qaytadan urinib ko'ring.")
            return

        await safe_edit_text(
            query.message,
            "📤 <b>YUBORILMOQDA...</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            f"{progress_bar(0)}  0%\n\n"
            f"👥 Jami: {len(subscribers)}\n\n"
            "<i>Iltimos, kuting...</i>",
            parse_mode=ParseMode.HTML,
        )

        natija = await send_to_all(
            context.bot,
            from_chat_id=data["from_chat_id"],
            message_id=data["message_id"],
            buttons=data.get("buttons"),
            progress_message=query.message,
        )

        daqiqa, soniya = divmod(natija["seconds"], 60)
        vaqt = f"{daqiqa} daq {soniya} son" if daqiqa else f"{soniya} soniya"
        foiz = round(natija["sent"] / natija["total"] * 100) if natija["total"] else 0

        hisobot = (
            "✅ <b>YUBORISH TUGADI</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            f"✅ Yetkazildi: <b>{natija['sent']}</b> ({foiz}%)\n"
        )
        if natija["blocked"]:
            hisobot += f"🚫 Bloklagan: <b>{natija['blocked']}</b> <i>(ro'yxatdan o'chirildi)</i>\n"
        if natija["errors"]:
            hisobot += f"⚠️ Xatolik: <b>{natija['errors']}</b>\n"
        hisobot += (
            f"\n⏱ Sarflangan vaqt: {vaqt}\n"
            f"👥 Qolgan obunachilar: <b>{len(subscribers)}</b>"
        )

        await safe_edit_text(
            query.message, hisobot, parse_mode=ParseMode.HTML, reply_markup=admin_menu_keyboard()
        )
        return

    if action == "cancel_broadcast":
        pending_broadcast.pop(ADMIN_ID, None)
        admin_state.pop(ADMIN_ID, None)
        await query.answer("Bekor qilindi")
        await safe_edit_text(
            query.message, "❌ Yuborish bekor qilindi.", reply_markup=admin_menu_keyboard()
        )
        return


# ---------------------------------------------------------
# Matn/media qabul qilish
# ---------------------------------------------------------
async def universal_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    message = update.message
    raw_text = (message.text or "").strip()

    # ⚠️ MUHIM: agar admin biror amalni kutayotgan bo'lsa (reklama qo'shish,
    # kanal qo'shish, xabar yuborish...), u BIRINCHI navbatda ishlanadi.
    # Aks holda admin yuborgan matn foydalanuvchi oqimiga (masalan ID
    # kutayotgan buyurtma bo'limiga) tushib qolishi mumkin.
    if user_id == ADMIN_ID and admin_state.get(ADMIN_ID):
        await handle_admin_input(update, context)
        return

    # --- Pastdagi doimiy klaviatura tugmalari ---
    if raw_text == "🎯 Mandat natijasini ko'rish":
        await message.reply_text(
            NATIJA_TEXT,
            parse_mode=ParseMode.HTML,
            reply_markup=natija_keyboard(),
            disable_web_page_preview=True,
        )
        await send_ad(context.bot, user_id, "result")
        return

    if raw_text == "🧮 Ball kalkulyatori":
        fans = get_all_fans()
        if not fans:
            await message.reply_text("⏳ Yo'nalishlar bazasi to'ldirilmoqda. Birozdan so'ng urinib ko'ring.")
            return
        await message.reply_text(
            "🧮 <b>BALL KALKULYATORI</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            "<b>1-qadam:</b> Fanlar majmuangizni tanlang 👇",
            parse_mode=ParseMode.HTML,
            reply_markup=build_fan_keyboard(),
        )
        return

    if raw_text == "📝 Mandatga buyurtma":
        user_order_state[user_id] = True
        await message.reply_text(ORDER_PROMPT, parse_mode=ParseMode.HTML)
        return

    if raw_text == "💰 Superkontrakt":
        user_super_state[user_id] = {"stage": "awaiting_farq"}
        await message.reply_text(
            "💰 <b>SUPERKONTRAKT HISOBLAGICHI</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            "<b>1-qadam:</b> O'tish balliga necha ball yetmaganini yozing\n\n"
            "<i>Masalan: 2.4</i>\n\n"
            "<i>(Ya'ni: o'tish balli 130, sizda 127.6 bo'lsa → 2.4 deb yozing)</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    if raw_text == "🔔 Eslatma":
        await show_main_menu(user_id, context.bot)
        return

    if raw_text == "ℹ️ Yordam":
        await message.reply_text(
            "ℹ️ <b>YORDAM</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            "🎯 <b>Mandat natijasini ko'rish</b>\n"
            "Rasmiy saytlarga to'g'ridan-to'g'ri o'tasiz: my.uzbmb.uz (pasport + "
            "JSHSHIR) yoki mandat.uzbmb.uz (ID raqami).\n\n"
            "📝 <b>Mandatga buyurtma</b>\n"
            "Abituriyent ID raqamingizni qoldirasiz. Natijalar e'lon qilinishi "
            "bilan bot sizga xabar va havola yuboradi.\n\n"
            "🧮 <b>Ball kalkulyatori</b>\n"
            "Fanlar majmuangizni tanlab, ballingizni kiritasiz — sizga mos "
            "yo'nalishlar ro'yxati chiqadi.\n\n"
            "💰 <b>Superkontrakt</b>\n"
            "Ball yetmagan bo'lsa, to'lov necha barobar bo'lishini hisoblaydi.\n\n"
            "🔔 <b>Eslatma</b>\n"
            "Yoqib qo'ysangiz, mandat natijalari e'lon qilinishi bilan bot sizga "
            "avtomatik xabar yuboradi.\n\n"
            "━━━━━━━━━━━━━━━\n"
            "📌 Ma'lumotlar 2025/2026 ko'rsatkichlari asosida taxminiy hisoblanadi.\n\n"
            "🔄 Botni qayta ishga tushirish: /start",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await send_ad(context.bot, user_id, "result")
        return

    # --- Ball kalkulyatori ---
    c_state = user_calc_state.get(user_id)
    if c_state and c_state.get("stage") == "awaiting_score":
        text = raw_text.replace(",", ".")
        try:
            score = float(text)
        except ValueError:
            await message.reply_text(
                "⚠️ Iltimos, faqat raqam yuboring.\n\n<i>Masalan: 165.3</i>",
                parse_mode=ParseMode.HTML,
            )
            return
        if score < 0 or score > 500:
            await message.reply_text("⚠️ Ball 0 va 500 oralig'ida bo'lishi kerak.")
            return
        fan = c_state.get("fan", "")
        user_calc_state.pop(user_id, None)
        again_kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄  Qayta hisoblash", callback_data="start_calc")],
                [InlineKeyboardButton("◀️  Bosh menyu", callback_data="calc_cancel")],
            ]
        )
        await message.reply_text(
            format_calc_result(score, fan),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=again_kb,
        )
        await send_ad(context.bot, user_id, "result")
        return

    # --- Yakuniy mandatga buyurtma (ID qabul qilish) ---
    if user_order_state.get(user_id):
        abit_id = re.sub(r"\D", "", raw_text)
        if not abit_id or not (5 <= len(abit_id) <= 9):
            await message.reply_text(
                "⚠️ Iltimos, faqat abituriyent ID raqamini yuboring (5–9 xonali son).\n\n"
                "<i>Masalan: 1234567</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        order_no, is_new = add_order(user_id, abit_id)
        # Buyurtma bergan foydalanuvchini avtomatik eslatmaga ham qo'shamiz
        if user_id not in subscribers:
            subscribers.add(user_id)
            save_json_set(SUBS_FILE, subscribers)

        user_order_state.pop(user_id, None)

        holat = "qabul qilindi" if is_new else "allaqachon ro'yxatda"
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕  Yana ID qo'shish", callback_data="start_order")],
                [InlineKeyboardButton("◀️  Bosh menyu", callback_data="calc_cancel")],
            ]
        )
        await message.reply_text(
            "✅ <b>Buyurtma " + holat + "!</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            f"🆔 Abituriyent ID: <b>{abit_id}</b>\n"
            f"📄 Buyurtma raqami: <b>{order_no}</b>\n\n"
            "🔔 Yakuniy mandat natijalari e'lon qilinishi bilan bot sizga "
            "<b>darhol xabar beradi</b> va natijani ko'rish havolasini yuboradi.\n\n"
            "<i>Bir nechta abituriyent uchun ketma-ket yuborishingiz mumkin.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        await send_ad(context.bot, user_id, "result")
        return

    # --- Superkontrakt hisoblagichi ---
    s_state = user_super_state.get(user_id)
    if s_state:
        text = raw_text.replace(",", ".").replace(" ", "")
        try:
            son = float(text)
        except ValueError:
            await message.reply_text(
                "⚠️ Iltimos, faqat raqam yuboring.", parse_mode=ParseMode.HTML
            )
            return

        if s_state.get("stage") == "awaiting_farq":
            if son < 0 or son > 200:
                await message.reply_text("⚠️ Noto'g'ri qiymat. Qaytadan urinib ko'ring.")
                return
            koef, _ = superkontrakt_koeffitsiyent(son)
            if koef is None or son <= 0:
                user_super_state.pop(user_id, None)
                await message.reply_text(
                    format_super_calc(son),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("◀️  Bosh menyu", callback_data="calc_cancel")]]
                    ),
                )
                await send_ad(context.bot, user_id, "result")
                return
            user_super_state[user_id] = {"stage": "awaiting_bazaviy", "farq": son}
            await message.reply_text(
                format_super_calc(son),
                parse_mode=ParseMode.HTML,
            )
            return

        if s_state.get("stage") == "awaiting_bazaviy":
            if son < 100000 or son > 200000000:
                await message.reply_text(
                    "⚠️ Kontrakt summasini so'mda yozing.\n\n<i>Masalan: 15000000</i>",
                    parse_mode=ParseMode.HTML,
                )
                return
            farq = s_state.get("farq", 0)
            user_super_state.pop(user_id, None)
            await message.reply_text(
                format_super_calc(farq, son),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🔄  Qayta hisoblash", callback_data="start_super")],
                        [InlineKeyboardButton("◀️  Bosh menyu", callback_data="calc_cancel")],
                    ]
                ),
            )
            await send_ad(context.bot, user_id, "result")
            return

    return


# ---------------------------------------------------------
# Admin kiritishlarini alohida ishlash
# ---------------------------------------------------------
async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    message = update.message
    raw_text = (message.text or "").strip()

    state = admin_state.get(ADMIN_ID)
    if not state:
        return

    if state == "awaiting_add_channel":
        text = (message.text or "").strip()
        if not text.startswith("@"):
            await message.reply_text("Kanal username'i @ bilan boshlanishi kerak.")
            return
        force_channels.add(text)
        save_json_set(CHANNELS_FILE, force_channels)
        admin_state.pop(ADMIN_ID, None)
        await message.reply_text(f"✅ {text} qo'shildi.", reply_markup=admin_menu_keyboard())
        return

    if state == "awaiting_add_program":
        lines = [ln.strip() for ln in (message.text or "").split("\n") if ln.strip()]
        added = 0
        for line in lines:
            if line.startswith("==="):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2:
                continue
            name = parts[0]
            try:
                ball = float(parts[1].replace(",", "."))
            except ValueError:
                continue
            fan = parts[2] if len(parts) > 2 else ""
            if not name:
                continue
            programs[uuid.uuid4().hex[:8]] = {"name": name, "ball": ball, "fan": fan}
            added += 1
        save_programs(programs)
        admin_state.pop(ADMIN_ID, None)
        await message.reply_text(
            f"✅ {added} ta yo'nalish qo'shildi.\nJami: {len(programs)} ta.",
            reply_markup=admin_menu_keyboard(),
        )
        return

    if state == "awaiting_ad_text":
        # message.text_html — Telegram'da siz qo'ygan formatlashni (qalin, qiya,
        # havola) tayyor HTML holida beradi. Ya'ni teglarni qo'lda yozish shart emas:
        # matnni belgilab, Telegram'ning o'z formatlash menyusidan foydalanasiz.
        new_text = (message.text_html or message.text or "").strip()
        if not new_text:
            await message.reply_text("⚠️ Bo'sh matn. Qaytadan yuboring.")
            return

        # Matn to'g'ri yuborilishini oldindan tekshiramiz
        try:
            preview = await message.reply_text(
                new_text, parse_mode=ParseMode.HTML, disable_web_page_preview=False
            )
        except BadRequest as e:
            await message.reply_text(
                "⚠️ <b>Matnda formatlash xatosi bor.</b>\n\n"
                f"<code>{str(e)[:200]}</code>\n\n"
                "Agar matnda <code>&lt;</code> yoki <code>&gt;</code> belgilari bo'lsa, "
                "ularni olib tashlang.\n\n"
                "Qaytadan yuboring yoki /admin bilan bekor qiling.",
                parse_mode=ParseMode.HTML,
            )
            return

        ads["items"][uuid.uuid4().hex[:8]] = {"text": new_text, "active": True, "views": 0}
        save_ads(ads)
        admin_state.pop(ADMIN_ID, None)
        await message.reply_text(
            "✅ <b>Reklama qo'shildi va faollashtirildi!</b>\n\n"
            "☝️ Yuqorida foydalanuvchilarga aynan shu ko'rinishda ko'rinadi.",
            parse_mode=ParseMode.HTML,
            reply_markup=ads_menu_keyboard(),
        )
        return

    if state == "awaiting_import":
        text = message.text or ""
        ids = {int(x) for x in re.findall(r"-?\d{5,}", text)}
        if not ids:
            await message.reply_text("Hech qanday ID topilmadi.")
            return
        before = len(subscribers)
        subscribers.update(ids)
        save_json_set(SUBS_FILE, subscribers)
        admin_state.pop(ADMIN_ID, None)
        await message.reply_text(
            f"✅ Import tugadi.\nTopildi: {len(ids)}\nYangi: {len(subscribers) - before}\n"
            f"Jami: {len(subscribers)}",
            reply_markup=admin_menu_keyboard(),
        )
        return

    if state == "awaiting_broadcast":
        # Xabarni "nusxa" usulida saqlaymiz — shunda formatlash (qalin matn,
        # havolalar), media turi va tugmalar to'liq saqlanadi.
        supported = (
            message.text or message.photo or message.video or message.document
            or message.audio or message.animation or message.voice or message.sticker
        )
        if not supported:
            await message.reply_text(
                "⚠️ Bu turdagi xabarni yubora olmayman.\n\n"
                "Matn, rasm, video, GIF, fayl, ovozli xabar yoki stiker yuboring."
            )
            return

        pending_broadcast[ADMIN_ID] = {
            "from_chat_id": message.chat_id,
            "message_id": message.message_id,
        }
        admin_state.pop(ADMIN_ID, None)

        # Xabar turini aniqlaymiz
        if message.photo:
            tur = "🖼 Rasm"
        elif message.video:
            tur = "🎬 Video"
        elif message.animation:
            tur = "🎞 GIF"
        elif message.document:
            tur = "📎 Fayl"
        elif message.audio:
            tur = "🎵 Audio"
        elif message.voice:
            tur = "🎤 Ovozli xabar"
        elif message.sticker:
            tur = "🩷 Stiker"
        else:
            tur = "📝 Matn"

        # Bot xabarni O'ZI qayta yuboradi — foydalanuvchi aynan shuni ko'radi
        await message.reply_text("👁 <b>Foydalanuvchilar shunday ko'radi:</b>", parse_mode=ParseMode.HTML)
        try:
            await context.bot.copy_message(
                chat_id=message.chat_id,
                from_chat_id=message.chat_id,
                message_id=message.message_id,
            )
        except Exception as e:
            logger.warning(f"Ko'rib chiqish yuborilmadi: {e}")

        confirm_kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔘  Tugma qo'shish", callback_data="bc_add_button")],
                [InlineKeyboardButton("✅  Ha, yuborilsin", callback_data="confirm_broadcast")],
                [InlineKeyboardButton("❌  Bekor qilish", callback_data="cancel_broadcast")],
            ]
        )
        await message.reply_text(
            "📢 <b>XABARNI TASDIQLASH</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            f"📄 Turi: <b>{tur}</b>\n"
            f"👥 Qabul qiluvchilar: <b>{len(subscribers)}</b> kishi\n"
            f"⏱ Taxminiy vaqt: <b>~{max(1, round(len(subscribers) / 20 / 60))} daqiqa</b>\n"
            "🔘 Tugmalar: <b>yo'q</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            "Yuborishni tasdiqlaysizmi?",
            parse_mode=ParseMode.HTML,
            reply_markup=confirm_kb,
        )
        return

    if state == "awaiting_bc_buttons":
        data = pending_broadcast.get(ADMIN_ID)
        if not data:
            admin_state.pop(ADMIN_ID, None)
            await message.reply_text("⚠️ Xabar topilmadi. Qaytadan boshlang: /admin")
            return

        tugmalar = parse_buttons(raw_text)
        if not tugmalar:
            await message.reply_text(
                "⚠️ <b>Format noto'g'ri.</b>\n\n"
                "Har bir tugmani yangi qatorga shunday yozing:\n\n"
                "<code>Tugma matni - https://havola</code>\n\n"
                "<b>Masalan:</b>\n"
                "<code>🎯 Natijani ko'rish - https://mandat.uzbmb.uz\n"
                "📢 Kanalimiz - https://t.me/kanal</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        data["buttons"] = tugmalar
        pending_broadcast[ADMIN_ID] = data
        admin_state.pop(ADMIN_ID, None)

        # Xabarni tugmalari bilan birga qayta ko'rsatamiz
        await message.reply_text(
            f"👁 <b>Foydalanuvchilar shunday ko'radi</b> ({len(tugmalar)} ta tugma bilan):",
            parse_mode=ParseMode.HTML,
        )
        try:
            await context.bot.copy_message(
                chat_id=message.chat_id,
                from_chat_id=data["from_chat_id"],
                message_id=data["message_id"],
                reply_markup=build_broadcast_keyboard(tugmalar),
            )
        except Exception as e:
            logger.warning(f"Ko'rib chiqish yuborilmadi: {e}")

        confirm_kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔘  Tugmalarni o'zgartirish", callback_data="bc_add_button")],
                [InlineKeyboardButton("🗑  Tugmalarni olib tashlash", callback_data="bc_clear_buttons")],
                [InlineKeyboardButton("✅  Ha, yuborilsin", callback_data="confirm_broadcast")],
                [InlineKeyboardButton("❌  Bekor qilish", callback_data="cancel_broadcast")],
            ]
        )
        await message.reply_text(
            "📢 <b>XABARNI TASDIQLASH</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            f"👥 Qabul qiluvchilar: <b>{len(subscribers)}</b> kishi\n"
            f"🔘 Tugmalar: <b>{len(tugmalar)} ta</b>\n"
            f"⏱ Taxminiy vaqt: <b>~{max(1, round(len(subscribers) / 20 / 60))} daqiqa</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            "Yuborishni tasdiqlaysizmi?",
            parse_mode=ParseMode.HTML,
            reply_markup=confirm_kb,
        )
        return


def parse_buttons(text: str) -> list[tuple[str, str]]:
    """'Matn - https://havola' ko'rinishidagi qatorlarni tugmaga aylantiradi."""
    natija = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # " - " yoki " | " ajratgichini qidiramiz
        ajratgich = None
        for sep in (" - ", " | ", " — ", "|"):
            if sep in line:
                ajratgich = sep
                break
        if not ajratgich:
            continue
        matn, _, havola = line.partition(ajratgich)
        matn, havola = matn.strip(), havola.strip()
        if not matn or not havola:
            continue
        # Havolani to'g'rilaymiz
        if havola.startswith("@"):
            havola = f"https://t.me/{havola[1:]}"
        elif havola.startswith("t.me/"):
            havola = f"https://{havola}"
        elif not havola.startswith(("http://", "https://")):
            continue
        natija.append((matn, havola))
        if len(natija) >= 10:
            break
    return natija


def build_broadcast_keyboard(buttons: list) -> InlineKeyboardMarkup | None:
    """Saqlangan tugmalardan klaviatura yasaydi."""
    if not buttons:
        return None
    rows = [[InlineKeyboardButton(matn, url=havola)] for matn, havola in buttons]
    return InlineKeyboardMarkup(rows)


async def send_to_all(bot, from_chat_id: int, message_id: int, buttons=None, progress_message=None):
    """Xabarni barcha obunachilarga nusxa qilib yuboradi.
    copy_message ishlatiladi — formatlash, media va emoji to'liq saqlanadi,
    va "forwarded from" yozuvi chiqmaydi."""
    sent, blocked_count, error_count = 0, 0, 0
    blocked: list[int] = []
    targets = list(subscribers)
    total = len(targets)
    BATCH = 20  # Telegram limitidan pastroq — xavfsizroq
    started = time.time()
    keyboard = build_broadcast_keyboard(buttons) if buttons else None

    async def send_one(chat_id: int) -> str:
        try:
            await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=from_chat_id,
                message_id=message_id,
                reply_markup=keyboard,
            )
            return "ok"
        except Forbidden:
            blocked.append(chat_id)
            return "blocked"
        except BadRequest as e:
            msg = str(e).lower()
            if "chat not found" in msg or "user is deactivated" in msg:
                blocked.append(chat_id)
                return "blocked"
            logger.warning(f"Yuborilmadi {chat_id}: {e}")
            return "error"
        except Exception as e:
            logger.warning(f"Yuborilmadi {chat_id}: {e}")
            return "error"

    for i in range(0, total, BATCH):
        chunk = targets[i:i + BATCH]
        results = await asyncio.gather(*(send_one(cid) for cid in chunk))
        sent += results.count("ok")
        blocked_count += results.count("blocked")
        error_count += results.count("error")

        # Har ~500 kishida admin'ga jarayonni ko'rsatamiz
        done = i + len(chunk)
        if progress_message and (done % 500 < BATCH) and done < total:
            foiz = round(done / total * 100)
            try:
                await safe_edit_text(
                    progress_message,
                    "📤 <b>YUBORILMOQDA...</b>\n"
                    "━━━━━━━━━━━━━━━\n\n"
                    f"{progress_bar(foiz)}  {foiz}%\n\n"
                    f"✅ Yuborildi: <b>{sent}</b>\n"
                    f"👥 Jami: {total}\n\n"
                    "<i>Iltimos, kuting...</i>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        await asyncio.sleep(1)  # Telegram limitiga rioya qilish

    # Botni bloklaganlarni ro'yxatdan bir marta o'chiramiz
    if blocked:
        for cid in blocked:
            subscribers.discard(cid)
        save_json_set(SUBS_FILE, subscribers)

    davomiylik = int(time.time() - started)
    return {
        "sent": sent,
        "blocked": blocked_count,
        "errors": error_count,
        "total": total,
        "seconds": davomiylik,
    }


def progress_bar(foiz: int, uzunlik: int = 10) -> str:
    to_ldi = round(foiz / 100 * uzunlik)
    return "█" * to_ldi + "░" * (uzunlik - to_ldi)


def main() -> None:
    app = (
        Application.builder()
        .token(TOKEN)
        # Bir vaqtda bir nechta foydalanuvchiga xizmat qiladi
        .concurrent_updates(True)
        # Ko'p foydalanuvchi bo'lganda ulanishlar navbati to'lib qolmasligi uchun
        .connection_pool_size(512)
        .pool_timeout(60)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .get_updates_pool_timeout(60)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("soni", count))
    app.add_handler(CallbackQueryHandler(check_subscription, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(toggle_subscription, pattern="^toggle_sub$"))
    app.add_handler(CallbackQueryHandler(start_calc, pattern="^start_calc$"))
    app.add_handler(CallbackQueryHandler(start_order, pattern="^start_order$"))
    app.add_handler(CallbackQueryHandler(show_natija, pattern="^show_natija$"))
    app.add_handler(CallbackQueryHandler(start_super, pattern="^start_super$"))
    app.add_handler(CallbackQueryHandler(calc_callback, pattern="^(calcfan:|calc_cancel)"))
    app.add_handler(CallbackQueryHandler(
        admin_callback,
        pattern="^(admin_|delch:|ad_|bc_|confirm_broadcast|cancel_broadcast)"
    ))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.VIDEO) & ~filters.COMMAND, universal_input_handler))
    app.add_error_handler(global_error_handler)

    logger.info("Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
