import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------
# SOZLAMALAR — Railway'dagi Variables bo'limidan o'qiladi.
# ---------------------------------------------------------
TOKEN = os.environ["TELEGRAM_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])

# AI tahlil funksiyasi uchun (ixtiyoriy). Bo'lmasa, "🎓 AI orqali tahlil"
# tugmasi ishlamaydi, lekin botning qolgan qismi normal ishlayveradi.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL = "claude-haiku-4-5-20251001"

# Boshlang'ich kanallar (ixtiyoriy) — keyinchalik admin panel orqali
# kanal qo'shish/o'chirish mumkin, Railway'ga qayta kirish shart emas.
SEED_CHANNELS = os.environ.get("FORCE_CHANNELS", "")

# Doimiy xotira papkasi. Railway'da Volume "/data" ga ulangan bo'lsa,
# ma'lumotlar deploy qilinganda ham o'chmaydi. Agar Volume bo'lmasa,
# oddiy papkaga yozadi (lekin bu holda deploy vaqtida yo'qolishi mumkin).
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BASE_DIR = DATA_DIR
except Exception:
    BASE_DIR = Path(__file__).parent
    logging.getLogger(__name__).warning(
        "DATA_DIR yozib bo'lmadi, vaqtinchalik papkaga saqlanadi — Volume ulanmagan bo'lishi mumkin."
    )

SUBS_FILE = BASE_DIR / "subscribers.json"
CHANNELS_FILE = BASE_DIR / "channels.json"
PROGRAMS_FILE = BASE_DIR / "programs.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# Ma'lumotlarni saqlash / o'qish
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

# Har bir adminning "hozir nima kutilyapti" holati (masalan: kanal kutilmoqda)
admin_state: dict[int, str] = {}
# Yuborishni kutayotgan xabar (tasdiqlashdan oldin)
pending_broadcast: dict[int, dict] = {}

# Yo'nalishlar ro'yxati: {id: "Universitet - Yo'nalish"}
def load_programs() -> dict:
    if PROGRAMS_FILE.exists():
        return json.loads(PROGRAMS_FILE.read_text())
    return {}


def save_programs(data: dict) -> None:
    PROGRAMS_FILE.write_text(json.dumps(data, ensure_ascii=False))


programs: dict[str, str] = load_programs()

# Har bir foydalanuvchining AI-tahlil oqimidagi holati:
# {"stage": "awaiting_score" | "selecting", "score": float, "selected": [id,...], "page": int}
user_analysis_state: dict[int, dict] = {}
PROGRAMS_PER_PAGE = 8


# ---------------------------------------------------------
# Majburiy obunani tekshirish
# ---------------------------------------------------------
async def get_missing_channels(bot, user_id: int) -> list[str]:
    missing = []
    for channel in force_channels:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in ("left", "kicked"):
                missing.append(channel)
        except Exception as e:
            logger.warning(f"{channel} tekshirilmadi: {e}")
            missing.append(channel)
    return missing


def build_subscription_keyboard(missing: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for channel in missing:
        username = channel.lstrip("@")
        rows.append(
            [InlineKeyboardButton(f"➕ {channel}", url=f"https://t.me/{username}")]
        )
    rows.append([InlineKeyboardButton("✅ Tekshirdim", callback_data="check_sub")])
    return InlineKeyboardMarkup(rows)


async def show_main_menu(chat_id: int, bot, edit_message=None) -> None:
    is_subscribed = chat_id in subscribers
    button_text = "🔕 Eslatmani o'chirish" if is_subscribed else "🔔 Eslatmani yoqish"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(button_text, callback_data="toggle_sub")],
            [InlineKeyboardButton("🎓 AI orqali natijani taxmin qilish", callback_data="start_analysis")],
        ]
    )
    text = (
        "📣 <b>Mandat natijalari haqida xabardor bo'lish</b>\n\n"
        "Yakuniy natijalar e'lon qilinganda tezkor xabar olish uchun "
        "quyidagi tugmani bosing.\n\n"
        "Shuningdek, to'plagan ballingiz asosida tanlagan yo'nalishlaringizga "
        "kirish ehtimolini sun'iy intellekt yordamida oldindan taxmin qilib ko'rishingiz mumkin."
    )
    if edit_message:
        await edit_message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


# ---------------------------------------------------------
# /start
# ---------------------------------------------------------
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

    missing = await get_missing_channels(context.bot, chat_id)
    if missing:
        await query.answer("Hali barcha kanallarga obuna bo'lmadingiz ❌", show_alert=True)
        await query.edit_message_reply_markup(reply_markup=build_subscription_keyboard(missing))
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
            await query.edit_message_reply_markup(reply_markup=build_subscription_keyboard(missing))
            return

    if chat_id in subscribers:
        subscribers.remove(chat_id)
        save_json_set(SUBS_FILE, subscribers)
        await query.answer("Eslatma o'chirildi")
        new_text = "🔔 Eslatmani yoqish"
        msg = "🔕 Siz obunani bekor qildingiz. Xohlasangiz, qayta yoqishingiz mumkin."
    else:
        subscribers.add(chat_id)
        save_json_set(SUBS_FILE, subscribers)
        await query.answer("Eslatma yoqildi ✅")
        new_text = "🔕 Eslatmani o'chirish"
        msg = "✅ Siz muvaffaqiyatli obuna bo'ldingiz! Natijalar e'lon qilinishi bilan sizga xabar boradi."

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(new_text, callback_data="toggle_sub")]]
    )
    await query.edit_message_text(msg, reply_markup=keyboard)


# ===========================================================
# AI ORQALI TAHLIL (foydalanuvchilar uchun)
# ===========================================================
def build_program_keyboard(selected: list[str], page: int) -> InlineKeyboardMarkup:
    ids = list(programs.keys())
    start = page * PROGRAMS_PER_PAGE
    page_ids = ids[start:start + PROGRAMS_PER_PAGE]

    rows = []
    for pid in page_ids:
        name = programs[pid]
        label = ("✅ " if pid in selected else "▫️ ") + (name[:45] + "…" if len(name) > 45 else name)
        rows.append([InlineKeyboardButton(label, callback_data=f"prog_toggle:{pid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"prog_page:{page-1}"))
    if start + PROGRAMS_PER_PAGE < len(ids):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"prog_page:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(f"✅ Tayyor ({len(selected)}/5)", callback_data="prog_done"),
            InlineKeyboardButton("❌ Bekor qilish", callback_data="prog_cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def start_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id

    if not ANTHROPIC_API_KEY:
        await query.answer()
        await query.message.reply_text(
            "⚠️ Bu funksiya hozircha sozlanmagan. Admin bilan bog'laning."
        )
        return

    if not programs:
        await query.answer()
        await query.message.reply_text("Hozircha yo'nalishlar ro'yxati kiritilmagan.")
        return

    user_analysis_state[chat_id] = {"stage": "awaiting_score"}
    await query.answer()
    await query.message.reply_text(
        "🎓 To'plagan umumiy ballingizni kiriting.\nMasalan: 165.3"
    )


async def analysis_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    action = query.data
    state = user_analysis_state.get(chat_id)

    if not state or state.get("stage") != "selecting":
        await query.answer("Sessiya tugagan. Qaytadan boshlang: /start", show_alert=True)
        return

    if action.startswith("prog_toggle:"):
        pid = action.split(":", 1)[1]
        selected = state["selected"]
        if pid in selected:
            selected.remove(pid)
        elif len(selected) < 5:
            selected.append(pid)
        else:
            await query.answer("Ko'pi bilan 5 ta yo'nalish tanlash mumkin ❌", show_alert=True)
            return
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=build_program_keyboard(selected, state["page"]))
        return

    if action.startswith("prog_page:"):
        state["page"] = int(action.split(":", 1)[1])
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=build_program_keyboard(state["selected"], state["page"]))
        return

    if action == "prog_cancel":
        user_analysis_state.pop(chat_id, None)
        await query.answer("Bekor qilindi")
        await query.edit_message_text("Bekor qilindi. Qayta boshlash uchun /start yozing.")
        return

    if action == "prog_done":
        if not state["selected"]:
            await query.answer("Kamida 1 ta yo'nalish tanlang ❌", show_alert=True)
            return
        await query.answer()
        chosen_names = [programs[pid] for pid in state["selected"] if pid in programs]
        score = state["score"]
        user_analysis_state.pop(chat_id, None)
        await query.edit_message_text("⏳ Tahlil qilinmoqda, biroz kuting...")

        try:
            result_text = await asyncio.to_thread(call_ai_analysis, score, chosen_names)
        except Exception as e:
            logger.warning(f"AI tahlil xatosi: {e}")
            result_text = "❌ Tahlil qilishda xatolik yuz berdi. Birozdan so'ng qayta urinib ko'ring."

        await context.bot.send_message(chat_id, result_text)
        return


def call_ai_analysis(score: float, chosen_programs: list[str]) -> str:
    programs_list = "\n".join(f"{i+1}. {name}" for i, name in enumerate(chosen_programs))
    system_prompt = (
        "Siz O'zbekistondagi oliy ta'lim muassasalariga qabul jarayoni bo'yicha "
        "yordamchi konsultantsiz. Foydalanuvchi to'plagan ball va tanlagan "
        "yo'nalishlari asosida, o'sha yo'nalishlarga kirish ehtimoli haqida "
        "taxminiy, ehtiyotkorlik bilan asoslangan fikr bering. Aniq rasmiy "
        "ma'lumotlar bazasiga ega emasligingizni va bu faqat taxminiy tahlil "
        "ekanini, rasmiy natija emasligini albatta ta'kidlang. Foydalanuvchini "
        "aniq va rasmiy ma'lumot uchun UZBMB (Bilim va malakalarni baholash "
        "agentligi)ning rasmiy 'mandat.uzbmb.uz' saytiga murojaat qilishni "
        "maslahat bering. O'zbek tilida, qisqa, tushunarli va foydalanuvchini "
        "keraksiz tashvishga solmaydigan uslubda yozing."
    )
    user_message = f"Mening to'plagan ballim: {score}\n\nTanlagan yo'nalishlarim:\n{programs_list}"

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": AI_MODEL,
            "max_tokens": 700,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    ai_text = data["content"][0]["text"]
    footer = (
        "\n\n📌 Diqqat: bu — sun'iy intellekt tomonidan berilgan taxminiy fikr, "
        "rasmiy natija emas. Aniq va rasmiy ma'lumotlar uchun UZBMB rasmiy saytiga murojaat qiling:\n"
        "https://mandat.uzbmb.uz/Bakalavr/BallInfoByResult"
    )
    return ai_text + footer


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
            [InlineKeyboardButton("🎓 Yo'nalishlar (AI tahlil uchun)", callback_data="admin_programs")],
            [InlineKeyboardButton("❌ Yopish", callback_data="admin_close")],
        ]
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
        await query.edit_message_text("Panel yopildi. Qayta ochish uchun /admin yozing.")
        return

    if action == "admin_stats":
        await query.answer()
        await query.edit_message_text(
            f"📊 <b>Statistika</b>\n\n"
            f"Obunachilar soni: {len(subscribers)}\n"
            f"Majburiy kanallar soni: {len(force_channels)}",
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
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_menu_keyboard())
        return

    if action == "admin_import":
        admin_state[ADMIN_ID] = "awaiting_import"
        await query.answer()
        await query.edit_message_text(
            "📥 Oldingi obunachilar ID ro'yxatini shu yerga yuboring "
            "(raqamlar, vergul yoki qator bilan ajratilgan holda — istalgan formatda bo'lishi mumkin).\n\n"
            "Bekor qilish uchun /admin yozing."
        )
        return

    if action == "admin_add_channel":
        admin_state[ADMIN_ID] = "awaiting_add_channel"
        await query.answer()
        await query.edit_message_text(
            "➕ Yangi kanal username'ini yuboring (masalan: @mening_kanalim).\n\n"
            "❗️ Bot o'sha kanalga <b>admin</b> qilib qo'shilgan bo'lishi kerak.\n"
            "Bekor qilish uchun /admin yozing.",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "admin_remove_channel":
        await query.answer()
        if not force_channels:
            await query.edit_message_text("Hozircha kanal yo'q.", reply_markup=admin_menu_keyboard())
            return
        rows = [
            [InlineKeyboardButton(f"🗑 {c}", callback_data=f"delch:{c}")] for c in force_channels
        ]
        rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_back")])
        await query.edit_message_text(
            "O'chirmoqchi bo'lgan kanalni tanlang:", reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if action == "admin_back":
        admin_state.pop(ADMIN_ID, None)
        await query.answer()
        await query.edit_message_text("🛠 <b>Admin panel</b>", parse_mode=ParseMode.HTML, reply_markup=admin_menu_keyboard())
        return

    if action.startswith("delch:"):
        channel = action.split(":", 1)[1]
        force_channels.discard(channel)
        save_json_set(CHANNELS_FILE, force_channels)
        await query.answer(f"{channel} o'chirildi ✅")
        await query.edit_message_text(
            f"✅ {channel} kanallar ro'yxatidan o'chirildi.", reply_markup=admin_menu_keyboard()
        )
        return

    if action == "admin_programs":
        await query.answer()
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Yo'nalish qo'shish", callback_data="admin_add_program")],
                [InlineKeyboardButton("➖ Yo'nalish o'chirish", callback_data="admin_remove_program")],
                [InlineKeyboardButton("📋 Ro'yxat", callback_data="admin_list_programs")],
                [InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_back")],
            ]
        )
        await query.edit_message_text(
            f"🎓 <b>Yo'nalishlar</b>\n\nHozircha: {len(programs)} ta yo'nalish kiritilgan.\n\n"
            "Bular foydalanuvchi AI-tahlil so'raganda tanlash uchun ro'yxatda chiqadi.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return

    if action == "admin_add_program":
        admin_state[ADMIN_ID] = "awaiting_add_program"
        await query.answer()
        await query.edit_message_text(
            "➕ Yangi yo'nalish nomini yuboring.\n"
            "Masalan: <i>TATU — Dasturiy injiniring</i>\n\n"
            "Bir nechtasini birdaniga qo'shish uchun har birini yangi qatordan yozing.\n\n"
            "Bekor qilish uchun /admin yozing.",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "admin_list_programs":
        await query.answer()
        if programs:
            text = "📋 <b>Yo'nalishlar:</b>\n\n" + "\n".join(f"• {v}" for v in programs.values())
        else:
            text = "📋 Hozircha yo'nalish yo'q."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_programs")]])
        await query.edit_message_text(text[:4000], parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == "admin_remove_program":
        await query.answer()
        if not programs:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_programs")]])
            await query.edit_message_text("Hozircha yo'nalish yo'q.", reply_markup=kb)
            return
        rows = [
            [InlineKeyboardButton(f"🗑 {name}", callback_data=f"delprog:{pid}")]
            for pid, name in programs.items()
        ]
        rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_programs")])
        await query.edit_message_text(
            "O'chirmoqchi bo'lgan yo'nalishni tanlang:", reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if action.startswith("delprog:"):
        pid = action.split(":", 1)[1]
        removed = programs.pop(pid, None)
        save_programs(programs)
        await query.answer("O'chirildi ✅" if removed else "Topilmadi")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_programs")]])
        msg = f"✅ {removed} o'chirildi." if removed else "Bu yo'nalish topilmadi."
        await query.edit_message_text(msg, reply_markup=kb)
        return

    if action == "admin_broadcast":
        admin_state[ADMIN_ID] = "awaiting_broadcast"
        await query.answer()
        await query.edit_message_text(
            "📢 Yubormoqchi bo'lgan xabaringizni yuboring:\n"
            "— Matn, yoki\n"
            "— Rasm/video (izoh bilan yoki izohsiz)\n\n"
            "Bekor qilish uchun /admin yozing."
        )
        return

    if action == "confirm_broadcast":
        await query.answer()
        data = pending_broadcast.pop(ADMIN_ID, None)
        if not data:
            await query.edit_message_text("Xabar topilmadi, qaytadan urinib ko'ring.")
            return
        await query.edit_message_text("⏳ Yuborilmoqda...")
        sent, failed = await send_to_all(context.bot, **data)
        await query.edit_message_text(f"✅ Yuborildi: {sent}\n❌ Xato: {failed}")
        return

    if action == "cancel_broadcast":
        pending_broadcast.pop(ADMIN_ID, None)
        admin_state.pop(ADMIN_ID, None)
        await query.answer("Bekor qilindi")
        await query.edit_message_text("Bekor qilindi.", reply_markup=admin_menu_keyboard())
        return


# ---------------------------------------------------------
# Matn/media xabarlarni holatga qarab yo'naltirish
# ---------------------------------------------------------
async def admin_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    message = update.message

    # Avval: foydalanuvchi AI-tahlil uchun ball kiritish oqimidami?
    a_state = user_analysis_state.get(user_id)
    if a_state and a_state.get("stage") == "awaiting_score":
        text = (message.text or "").strip().replace(",", ".")
        try:
            score = float(text)
        except ValueError:
            await message.reply_text("Iltimos, faqat raqam yuboring. Masalan: 165.3")
            return
        if not programs:
            await message.reply_text("Hozircha yo'nalishlar ro'yxati kiritilmagan. Birozdan so'ng urinib ko'ring.")
            user_analysis_state.pop(user_id, None)
            return
        user_analysis_state[user_id] = {"stage": "selecting", "score": score, "selected": [], "page": 0}
        await message.reply_text(
            f"Ball qabul qilindi: {score}\n\nEndi kamida 1, ko'pi bilan 5 ta yo'nalishni tanlang:",
            reply_markup=build_program_keyboard([], 0),
        )
        return

    if user_id != ADMIN_ID:
        return

    state = admin_state.get(ADMIN_ID)
    if not state:
        return  # Admin oddiy foydalanuvchi kabi yozayotgan bo'lishi mumkin

    if state == "awaiting_add_channel":
        text = (message.text or "").strip()
        if not text.startswith("@"):
            await message.reply_text("Kanal username'i @ bilan boshlanishi kerak. Masalan: @mening_kanalim")
            return
        force_channels.add(text)
        save_json_set(CHANNELS_FILE, force_channels)
        admin_state.pop(ADMIN_ID, None)
        await message.reply_text(f"✅ {text} qo'shildi.", reply_markup=admin_menu_keyboard())
        return

    if state == "awaiting_add_program":
        lines = [ln.strip() for ln in (message.text or "").split("\n") if ln.strip()]
        if not lines:
            await message.reply_text("Bo'sh matn. Qaytadan yuboring.")
            return
        for line in lines:
            programs[uuid.uuid4().hex[:8]] = line
        save_programs(programs)
        admin_state.pop(ADMIN_ID, None)
        await message.reply_text(
            f"✅ {len(lines)} ta yo'nalish qo'shildi. Jami: {len(programs)} ta.",
            reply_markup=admin_menu_keyboard(),
        )
        return

    if state == "awaiting_import":
        import re
        text = message.text or ""
        ids = {int(x) for x in re.findall(r"-?\d{5,}", text)}
        if not ids:
            await message.reply_text("Hech qanday ID topilmadi. Qaytadan yuboring yoki /admin bilan bekor qiling.")
            return
        before = len(subscribers)
        subscribers.update(ids)
        save_json_set(SUBS_FILE, subscribers)
        added = len(subscribers) - before
        admin_state.pop(ADMIN_ID, None)
        await message.reply_text(
            f"✅ Import tugadi.\nTopildi: {len(ids)}\nYangi qo'shildi: {added}\n"
            f"Jami obunachilar: {len(subscribers)}",
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
            await message.reply_text("Bu turdagi xabarni yubora olmayman. Matn, rasm yoki video yuboring.")
            return

        pending_broadcast[ADMIN_ID] = data
        admin_state.pop(ADMIN_ID, None)

        preview = data.get("text", "")[:200] or "(izohsiz media)"
        confirm_kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Yuborish", callback_data="confirm_broadcast"),
                    InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_broadcast"),
                ]
            ]
        )
        await message.reply_text(
            f"Quyidagi xabar {len(subscribers)} kishiga yuboriladi:\n\n{preview}\n\nTasdiqlaysizmi?",
            reply_markup=confirm_kb,
        )
        return


async def send_to_all(
    bot, text: str = "", photo_id: str | None = None, video_id: str | None = None
) -> tuple[int, int]:
    sent, failed = 0, 0
    for chat_id in list(subscribers):
        try:
            if photo_id:
                await bot.send_photo(chat_id=chat_id, photo=photo_id, caption=text or None)
            elif video_id:
                await bot.send_video(chat_id=chat_id, video=video_id, caption=text or None)
            else:
                await bot.send_message(chat_id=chat_id, text=text)
            sent += 1
        except Forbidden:
            subscribers.discard(chat_id)
            save_json_set(SUBS_FILE, subscribers)
            failed += 1
        except Exception as e:
            logger.warning(f"Xabar yuborilmadi {chat_id}: {e}")
            failed += 1
    logger.info(f"Broadcast: yuborildi={sent}, xato={failed}")
    return sent, failed


async def count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(f"Hozirgi obunachilar soni: {len(subscribers)}")


def main() -> None:
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("soni", count))
    app.add_handler(CallbackQueryHandler(check_subscription, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(toggle_subscription, pattern="^toggle_sub$"))
    app.add_handler(CallbackQueryHandler(start_analysis, pattern="^start_analysis$"))
    app.add_handler(CallbackQueryHandler(analysis_callback, pattern="^(prog_toggle:|prog_page:|prog_done|prog_cancel)"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^(admin_|delch:|delprog:|confirm_broadcast|cancel_broadcast)"))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.VIDEO) & ~filters.COMMAND, admin_input_handler))

    logger.info("Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
