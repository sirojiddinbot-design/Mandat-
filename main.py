import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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


def append_ad(text: str, place: str) -> str:
    """place: 'start' yoki 'result'"""
    key = "show_on_start" if place == "start" else "show_on_result"
    if not ads.get(key):
        return text
    result = get_active_ad()
    if not result:
        return text
    ad_id, ad_text = result
    register_ad_view(ad_id)
    return f"{text}\n\n➖➖➖➖➖\n{ad_text}"


# ---------------------------------------------------------
# Majburiy obuna (kesh bilan — tezkor ishlashi uchun)
# ---------------------------------------------------------
# Foydalanuvchi a'zoligi natijasini vaqtincha eslab qolamiz,
# shunda har tugma bosilganda Telegram'ga qayta so'rov ketmaydi.
_membership_cache: dict[int, tuple[float, list[str]]] = {}
MEMBERSHIP_CACHE_SECONDS = 300  # 5 daqiqa


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
        except Exception as e:
            logger.warning(f"{channel} tekshirilmadi: {e}")
            return channel

    results = await asyncio.gather(*(check(c) for c in force_channels))
    missing = [c for c in results if c]

    _membership_cache[user_id] = (now, missing)
    return missing


def build_subscription_keyboard(missing: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for channel in missing:
        username = channel.lstrip("@")
        rows.append([InlineKeyboardButton(f"➕ {channel}", url=f"https://t.me/{username}")])
    rows.append([InlineKeyboardButton("✅ Tekshirdim", callback_data="check_sub")])
    return InlineKeyboardMarkup(rows)


async def show_main_menu(chat_id: int, bot, edit_message=None) -> None:
    is_subscribed = chat_id in subscribers
    button_text = "🔕 Eslatmani o'chirish" if is_subscribed else "🔔 Eslatmani yoqish"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(button_text, callback_data="toggle_sub")],
            [InlineKeyboardButton("🧮 Ball kalkulyatori", callback_data="start_calc")],
        ]
    )
    text = (
        "📣 <b>Mandat natijalari haqida xabardor bo'lish</b>\n\n"
        "Quyidagi tugmalardan birini tanlang:\n\n"
        "🔔 <b>Eslatmani yoqish</b> — yakuniy natijalar e'lon qilinganda tezkor xabar olasiz\n"
        "🧮 <b>Ball kalkulyatori</b> — to'plagan ballingiz asosida mos yo'nalishlarni bilib olasiz"
    )
    text = append_ad(text, "start")

    if edit_message:
        await safe_edit_text(edit_message, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if force_channels:
        missing = await get_missing_channels(context.bot, chat_id)
        if missing:
            await update.message.reply_text(
                "⚠️ Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling, "
                "so'ng «✅ Tekshirdim» tugmasini bosing:",
                reply_markup=build_subscription_keyboard(missing),
            )
            return
    await show_main_menu(chat_id, context.bot)


async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    # "Tekshirdim" bosilganda keshni ishlatmaymiz — foydalanuvchi hozirgina
    # obuna bo'lgan bo'lishi mumkin, shuning uchun yangidan tekshiramiz.
    missing = await get_missing_channels(context.bot, chat_id, use_cache=False)
    if missing:
        await query.answer("Hali barcha kanallarga obuna bo'lmadingiz ❌", show_alert=True)
        await safe_edit_markup(query.message, reply_markup=build_subscription_keyboard(missing))
        return
    await query.answer("Obuna tasdiqlandi ✅")
    await show_main_menu(chat_id, context.bot, edit_message=query.message)


async def toggle_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    if force_channels:
        missing = await get_missing_channels(context.bot, chat_id)
        if missing:
            await query.answer("Avval kanallarga obuna bo'ling ❌", show_alert=True)
            await safe_edit_markup(query.message, reply_markup=build_subscription_keyboard(missing))
            return
    if chat_id in subscribers:
        subscribers.remove(chat_id)
        save_json_set(SUBS_FILE, subscribers)
        await query.answer("Eslatma o'chirildi")
    else:
        subscribers.add(chat_id)
        save_json_set(SUBS_FILE, subscribers)
        await query.answer("Eslatma yoqildi ✅")
    await show_main_menu(chat_id, context.bot, edit_message=query.message)


# ===========================================================
# BALL KALKULYATORI (fan majmuasi bilan)
# ===========================================================
def get_all_fans() -> list[str]:
    fans = {p["fan"] for p in programs.values() if p.get("fan") and p.get("ball") is not None}
    return sorted(fans)


def build_fan_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f, callback_data=f"calcfan:{f}")] for f in get_all_fans()]
    rows.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="calc_cancel")])
    return InlineKeyboardMarkup(rows)


async def start_calc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    fans = get_all_fans()
    if not fans:
        await query.answer()
        await query.message.reply_text(
            "Hozircha yo'nalishlar bazasi to'ldirilmagan. Birozdan so'ng qayta urinib ko'ring."
        )
        return
    await query.answer()
    await query.message.reply_text(
        "🧮 <b>Ball kalkulyatori</b>\n\nAvval fanlar majmuangizni tanlang:",
        parse_mode=ParseMode.HTML,
        reply_markup=build_fan_keyboard(),
    )


async def calc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    action = query.data

    if action == "calc_cancel":
        user_calc_state.pop(chat_id, None)
        await query.answer("Bekor qilindi")
        await safe_edit_text(query.message, "Bekor qilindi. Qayta boshlash uchun /start yozing.")
        return

    if action.startswith("calcfan:"):
        fan = action.split(":", 1)[1]
        user_calc_state[chat_id] = {"stage": "awaiting_score", "fan": fan}
        await query.answer()
        await safe_edit_text(
            query.message,
            f"✅ Tanlangan fanlar: <b>{fan}</b>\n\n"
            "Endi to'plagan umumiy ballingizni kiriting.\nMasalan: 165.3",
            parse_mode=ParseMode.HTML,
        )
        return


def format_calc_result(score: float, fan: str) -> str:
    matches = [
        p for p in programs.values()
        if p.get("ball") is not None and p.get("fan") == fan and p["ball"] <= score
    ]
    matches.sort(key=lambda p: p["ball"], reverse=True)

    all_in_fan = [p for p in programs.values() if p.get("fan") == fan and p.get("ball") is not None]

    if not matches:
        min_ball = min((p["ball"] for p in all_in_fan), default=None)
        body = (
            f"🧮 Ballingiz: {score}\n"
            f"📚 Fanlar: {fan}\n\n"
            "❌ Afsuski, bu fanlar majmuasida ballingizga mos yo'nalish topilmadi."
        )
        if min_ball is not None:
            body += f"\n\nEng past o'tish balli: {min_ball}"
    else:
        lines = [
            f"🧮 Ballingiz: {score}",
            f"📚 Fanlar: {fan}",
            "",
            f"✅ Mos keladigan yo'nalishlar ({len(matches)} ta):",
            "",
        ]
        for p in matches[:30]:
            farq = round(score - p["ball"], 1)
            lines.append(f"• {p['name']}\n   O'tish balli: {p['ball']} (sizda +{farq})")
        if len(matches) > 30:
            lines.append(f"\n... va yana {len(matches) - 30} ta yo'nalish.")
        body = "\n".join(lines)

    body += (
        "\n\n📌 Bu ma'lumotlar 2025/2026 o'quv yili ko'rsatkichlari asosida berilmoqda "
        "va taxminiy xarakterga ega. Rasmiy ma'lumot uchun: https://mandat.uzbmb.uz"
    )
    return append_ad(body, "result")


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
        "📣 <b>Reklama bo'limi</b>\n\n"
        f"📊 Jami reklamalar: {total} ta\n"
        f"✅ Faol: {active} ta\n"
        f"👁 Jami ko'rishlar: {views} ta\n\n"
        "⚙️ Sozlamalar:\n"
        f"🚀 Start'da: {'✅ Yoniq' if ads.get('show_on_start') else '❌ O`chiq'}\n"
        f"🧮 Natijada: {'✅ Yoniq' if ads.get('show_on_result') else '❌ O`chiq'}"
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    admin_state.pop(ADMIN_ID, None)
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
            "➕ Yangi reklama matnini yuboring.\n\n"
            "Havola qo'shsangiz ham bo'ladi.\n"
            "Bekor qilish uchun /admin yozing.",
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
            "📢 Yubormoqchi bo'lgan xabaringizni yuboring:\n"
            "— Matn, rasm yoki video\n\n"
            "Bekor qilish uchun /admin yozing.",
        )
        return

    if action == "confirm_broadcast":
        await query.answer()
        data = pending_broadcast.pop(ADMIN_ID, None)
        if not data:
            await safe_edit_text(query.message, "Xabar topilmadi.")
            return
        await safe_edit_text(query.message, "⏳ Yuborilmoqda...")
        sent, failed = await send_to_all(context.bot, **data)
        await safe_edit_text(query.message, f"✅ Yuborildi: {sent}\n❌ Xato: {failed}")
        return

    if action == "cancel_broadcast":
        pending_broadcast.pop(ADMIN_ID, None)
        admin_state.pop(ADMIN_ID, None)
        await query.answer("Bekor qilindi")
        await safe_edit_text(query.message, "Bekor qilindi.", reply_markup=admin_menu_keyboard())
        return


# ---------------------------------------------------------
# Matn/media qabul qilish
# ---------------------------------------------------------
async def universal_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    message = update.message

    # Ball kalkulyatori
    c_state = user_calc_state.get(user_id)
    if c_state and c_state.get("stage") == "awaiting_score":
        text = (message.text or "").strip().replace(",", ".")
        try:
            score = float(text)
        except ValueError:
            await message.reply_text("Iltimos, faqat raqam yuboring. Masalan: 165.3")
            return
        fan = c_state.get("fan", "")
        user_calc_state.pop(user_id, None)
        await message.reply_text(format_calc_result(score, fan), disable_web_page_preview=True)
        return

    if user_id != ADMIN_ID:
        return

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
        new_text = (message.text or "").strip()
        if not new_text:
            await message.reply_text("Bo'sh matn. Qaytadan yuboring.")
            return
        ads["items"][uuid.uuid4().hex[:8]] = {"text": new_text, "active": True, "views": 0}
        save_ads(ads)
        admin_state.pop(ADMIN_ID, None)
        await message.reply_text("✅ Reklama qo'shildi va faollashtirildi.", reply_markup=ads_menu_keyboard())
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
        data = {}
        if message.photo:
            data["photo_id"] = message.photo[-1].file_id
            data["text"] = message.caption or ""
        elif message.video:
            data["video_id"] = message.video.file_id
            data["text"] = message.caption or ""
        elif message.text:
            data["text"] = message.text
        else:
            await message.reply_text("Matn, rasm yoki video yuboring.")
            return

        pending_broadcast[ADMIN_ID] = data
        admin_state.pop(ADMIN_ID, None)
        preview = data.get("text", "")[:200] or "(izohsiz media)"
        confirm_kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Yuborish", callback_data="confirm_broadcast"),
                InlineKeyboardButton("❌ Bekor", callback_data="cancel_broadcast"),
            ]]
        )
        await message.reply_text(
            f"Quyidagi xabar {len(subscribers)} kishiga yuboriladi:\n\n{preview}\n\nTasdiqlaysizmi?",
            reply_markup=confirm_kb,
        )
        return


async def send_to_all(bot, text: str = "", photo_id: str | None = None, video_id: str | None = None):
    """Xabarni barcha obunachilarga yuboradi.
    Telegram sekundiga ~30 ta xabarga ruxsat beradi, shuning uchun
    kichik guruhlarga bo'lib, orasida qisqa pauza bilan yuboramiz —
    bu ham tez, ham bloklanmaydi."""
    sent, failed = 0, 0
    blocked: list[int] = []
    targets = list(subscribers)
    BATCH = 25

    async def send_one(chat_id: int) -> str:
        try:
            if photo_id:
                await bot.send_photo(chat_id=chat_id, photo=photo_id, caption=text or None)
            elif video_id:
                await bot.send_video(chat_id=chat_id, video=video_id, caption=text or None)
            else:
                await bot.send_message(chat_id=chat_id, text=text)
            return "ok"
        except Forbidden:
            blocked.append(chat_id)
            return "blocked"
        except Exception as e:
            logger.warning(f"Yuborilmadi {chat_id}: {e}")
            return "error"

    for i in range(0, len(targets), BATCH):
        chunk = targets[i:i + BATCH]
        results = await asyncio.gather(*(send_one(cid) for cid in chunk))
        sent += results.count("ok")
        failed += len(results) - results.count("ok")
        await asyncio.sleep(1)  # Telegram limitiga rioya qilish

    # Botni bloklagan foydalanuvchilarni ro'yxatdan bir marta o'chiramiz
    if blocked:
        for cid in blocked:
            subscribers.discard(cid)
        save_json_set(SUBS_FILE, subscribers)

    return sent, failed


def main() -> None:
    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)  # Bir vaqtda bir nechta foydalanuvchiga xizmat qiladi
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("soni", count))
    app.add_handler(CallbackQueryHandler(check_subscription, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(toggle_subscription, pattern="^toggle_sub$"))
    app.add_handler(CallbackQueryHandler(start_calc, pattern="^start_calc$"))
    app.add_handler(CallbackQueryHandler(calc_callback, pattern="^(calcfan:|calc_cancel)"))
    app.add_handler(CallbackQueryHandler(
        admin_callback,
        pattern="^(admin_|delch:|ad_|confirm_broadcast|cancel_broadcast)"
    ))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.VIDEO) & ~filters.COMMAND, universal_input_handler))
    app.add_error_handler(global_error_handler)

    logger.info("Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
