import json
import logging
import os
import re
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
# SOZLAMALAR — Railway'dagi Variables bo'limidan o'qiladi.
# ---------------------------------------------------------
TOKEN = os.environ["TELEGRAM_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])

# Boshlang'ich kanallar (ixtiyoriy) — keyinchalik admin panel orqali
# kanal qo'shish/o'chirish mumkin, Railway'ga qayta kirish shart emas.
SEED_CHANNELS = os.environ.get("FORCE_CHANNELS", "")

# Doimiy xotira papkasi. Railway'da Volume "/data" ga ulangan bo'lsa,
# ma'lumotlar deploy qilinganda ham o'chmaydi.
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
AD_FILE = BASE_DIR / "ad.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# Xavfsiz tahrirlash — "Message is not modified" xatosi butun
# botni yiqitib qo'ymasligi uchun (masalan bir xil tugma ketma-ket
# bosilganda Telegram shu xatoni qaytaradi).
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

# Har bir adminning "hozir nima kutilyapti" holati
admin_state: dict[int, str] = {}
# Yuborishni kutayotgan xabar (tasdiqlashdan oldin)
pending_broadcast: dict[int, dict] = {}


# ---------------------------------------------------------
# Yo'nalishlar ro'yxati: {id: {"name": str, "ball": float|None}}
# ---------------------------------------------------------
def load_programs() -> dict:
    if not PROGRAMS_FILE.exists():
        return {}
    raw = json.loads(PROGRAMS_FILE.read_text())
    # Eski formatdan (id -> matn) yangi formatga (id -> {"name","ball"}) o'tkazish
    fixed = {}
    for pid, val in raw.items():
        if isinstance(val, dict):
            fixed[pid] = {"name": val.get("name", ""), "ball": val.get("ball")}
        else:
            fixed[pid] = {"name": val, "ball": None}
    return fixed


def save_programs(data: dict) -> None:
    PROGRAMS_FILE.write_text(json.dumps(data, ensure_ascii=False))


programs: dict[str, dict] = load_programs()


# ---------------------------------------------------------
# Reklama matni (admin sozlaydi, ball kalkulyatori natijasiga qo'shiladi)
# ---------------------------------------------------------
def load_ad_text() -> str:
    if AD_FILE.exists():
        return json.loads(AD_FILE.read_text()).get("text", "")
    return ""


def save_ad_text(text: str) -> None:
    AD_FILE.write_text(json.dumps({"text": text}, ensure_ascii=False))


ad_text: str = load_ad_text()

# Har bir foydalanuvchining ball-kalkulyator oqimidagi holati
user_calc_state: dict[int, dict] = {}


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
            [InlineKeyboardButton("🧮 Ball kalkulyatori", callback_data="start_calc")],
        ]
    )
    text = (
        "📣 <b>Mandat natijalari haqida xabardor bo'lish</b>\n\n"
        "Quyidagi tugmalardan birini tanlang:\n\n"
        "🔔 <b>Eslatmani yoqish</b> — yakuniy natijalar e'lon qilinganda tezkor xabar olasiz\n"
        "🧮 <b>Ball kalkulyatori</b> — to'plagan ballingiz asosida mos yo'nalishlarni bilib olasiz"
    )
    if edit_message:
        await safe_edit_text(edit_message, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
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
# BALL KALKULYATORI (foydalanuvchilar uchun)
# ===========================================================
async def start_calc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id

    scored = [p for p in programs.values() if p.get("ball") is not None]
    if not scored:
        await query.answer()
        await query.message.reply_text(
            "Hozircha yo'nalishlar bo'yicha ball ma'lumotlari kiritilmagan. "
            "Birozdan so'ng qayta urinib ko'ring."
        )
        return

    user_calc_state[chat_id] = {"stage": "awaiting_score"}
    await query.answer()
    await query.message.reply_text(
        "🧮 To'plagan umumiy ballingizni kiriting.\nMasalan: 165.3"
    )


def format_calc_result(score: float) -> str:
    matches = [
        p for p in programs.values()
        if p.get("ball") is not None and p["ball"] <= score
    ]
    matches.sort(key=lambda p: p["ball"], reverse=True)

    if not matches:
        body = (
            f"🧮 Ballingiz: {score}\n\n"
            "Afsuski, hozircha kiritilgan yo'nalishlar orasida ballingizga mos "
            "keladigani topilmadi."
        )
    else:
        top = matches[:25]
        lines = [f"🧮 Ballingiz: {score}", f"✅ Mos keladigan yo'nalishlar ({len(matches)} ta topildi):", ""]
        for p in top:
            lines.append(f"• {p['name']} — {p['ball']}")
        if len(matches) > 25:
            lines.append(f"\n... va yana {len(matches) - 25} ta yo'nalish.")
        body = "\n".join(lines)

    body += (
        "\n\n📌 Bu ma'lumotlar o'tgan yilgi (taxminiy) ko'rsatkichlar asosida "
        "berilmoqda, rasmiy natija emas."
    )

    if ad_text:
        body += f"\n\n{ad_text}"

    return body


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
            [InlineKeyboardButton("🎓 Yo'nalishlar (ball bazasi)", callback_data="admin_programs")],
            [InlineKeyboardButton("📝 Reklama matni", callback_data="admin_ad")],
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
        await safe_edit_text(query.message, "Panel yopildi. Qayta ochish uchun /admin yozing.")
        return

    if action == "admin_stats":
        await query.answer()
        scored = sum(1 for p in programs.values() if p.get("ball") is not None)
        await safe_edit_text(query.message, 
            f"📊 <b>Statistika</b>\n\n"
            f"Obunachilar soni: {len(subscribers)}\n"
            f"Majburiy kanallar soni: {len(force_channels)}\n"
            f"Yo'nalishlar soni: {len(programs)} (balli: {scored})",
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
        await safe_edit_text(query.message, 
            "📥 Oldingi obunachilar ID ro'yxatini shu yerga yuboring "
            "(raqamlar, vergul yoki qator bilan ajratilgan holda — istalgan formatda bo'lishi mumkin).\n\n"
            "Bekor qilish uchun /admin yozing."
        )
        return

    if action == "admin_add_channel":
        admin_state[ADMIN_ID] = "awaiting_add_channel"
        await query.answer()
        await safe_edit_text(query.message, 
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
        rows = [
            [InlineKeyboardButton(f"🗑 {c}", callback_data=f"delch:{c}")] for c in force_channels
        ]
        rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_back")])
        await safe_edit_text(query.message, 
            "O'chirmoqchi bo'lgan kanalni tanlang:", reply_markup=InlineKeyboardMarkup(rows)
        )
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
        await safe_edit_text(query.message, 
            f"✅ {channel} kanallar ro'yxatidan o'chirildi.", reply_markup=admin_menu_keyboard()
        )
        return

    if action == "admin_programs":
        await query.answer()
        scored = sum(1 for p in programs.values() if p.get("ball") is not None)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Yo'nalish qo'shish", callback_data="admin_add_program")],
                [InlineKeyboardButton("➖ Yo'nalish o'chirish", callback_data="admin_remove_program")],
                [InlineKeyboardButton("📋 Ro'yxat", callback_data="admin_list_programs")],
                [InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_back")],
            ]
        )
        await safe_edit_text(query.message, 
            f"🎓 <b>Yo'nalishlar</b>\n\nJami: {len(programs)} ta, balli kiritilgan: {scored} ta.\n\n"
            "Faqat balli kiritilgan yo'nalishlar Ball kalkulyatorida ishlatiladi.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return

    if action == "admin_add_program":
        admin_state[ADMIN_ID] = "awaiting_add_program"
        await query.answer()
        await safe_edit_text(query.message, 
            "➕ Yangi yo'nalishlarni yuboring.\n\n"
            "Format: <b>Nomi | Ball</b> (har biri yangi qatordan)\n"
            "Masalan:\n"
            "<code>TATU — Dasturiy injiniring | 165.2\n"
            "ToshDTU — Iqtisodiyot | 148.7</code>\n\n"
            "Ball kiritmasangiz ham bo'ladi (faqat nom yozing), lekin bunday "
            "yo'nalish Ball kalkulyatorida ishlatilmaydi.\n\n"
            "Bekor qilish uchun /admin yozing.",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "admin_list_programs":
        await query.answer()
        if programs:
            lines = []
            for p in programs.values():
                ball_str = str(p["ball"]) if p.get("ball") is not None else "ball yo'q"
                lines.append(f"• {p['name']} — {ball_str}")
            text = "📋 <b>Yo'nalishlar:</b>\n\n" + "\n".join(lines)
        else:
            text = "📋 Hozircha yo'nalish yo'q."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_programs")]])
        await safe_edit_text(query.message, text[:4000], parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == "admin_remove_program":
        await query.answer()
        if not programs:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_programs")]])
            await safe_edit_text(query.message, "Hozircha yo'nalish yo'q.", reply_markup=kb)
            return
        rows = [
            [InlineKeyboardButton(f"🗑 {p['name']}", callback_data=f"delprog:{pid}")]
            for pid, p in programs.items()
        ]
        rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_programs")])
        await safe_edit_text(query.message, 
            "O'chirmoqchi bo'lgan yo'nalishni tanlang:", reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if action.startswith("delprog:"):
        pid = action.split(":", 1)[1]
        removed = programs.pop(pid, None)
        save_programs(programs)
        await query.answer("O'chirildi ✅" if removed else "Topilmadi")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_programs")]])
        msg = f"✅ {removed['name']} o'chirildi." if removed else "Bu yo'nalish topilmadi."
        await safe_edit_text(query.message, msg, reply_markup=kb)
        return

    if action == "admin_ad":
        admin_state[ADMIN_ID] = "awaiting_ad_text"
        await query.answer()
        current = ad_text if ad_text else "(hozircha o'rnatilmagan)"
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🗑 Reklamani o'chirish", callback_data="admin_ad_clear")]]
        )
        await safe_edit_text(query.message, 
            f"📝 Hozirgi reklama matni:\n\n{current}\n\n"
            "Ball kalkulyatori natijasi oxiriga qo'shiladigan yangi matnni yuboring.\n\n"
            "Bekor qilish uchun /admin yozing.",
            reply_markup=kb,
        )
        return

    if action == "admin_ad_clear":
        global ad_text
        ad_text = ""
        save_ad_text("")
        admin_state.pop(ADMIN_ID, None)
        await query.answer("Reklama o'chirildi ✅")
        await safe_edit_text(query.message, "✅ Reklama matni o'chirildi.", reply_markup=admin_menu_keyboard())
        return

    if action == "admin_broadcast":
        admin_state[ADMIN_ID] = "awaiting_broadcast"
        await query.answer()
        await safe_edit_text(query.message, 
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
            await safe_edit_text(query.message, "Xabar topilmadi, qaytadan urinib ko'ring.")
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
# Matn/media xabarlarni holatga qarab yo'naltirish
# ---------------------------------------------------------
async def universal_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    message = update.message

    # 1) Foydalanuvchi Ball kalkulyatorida ball kutilyaptimi?
    c_state = user_calc_state.get(user_id)
    if c_state and c_state.get("stage") == "awaiting_score":
        text = (message.text or "").strip().replace(",", ".")
        try:
            score = float(text)
        except ValueError:
            await message.reply_text("Iltimos, faqat raqam yuboring. Masalan: 165.3")
            return
        user_calc_state.pop(user_id, None)
        await message.reply_text(format_calc_result(score))
        return

    # 2) Qolgani faqat admin uchun
    if user_id != ADMIN_ID:
        return

    state = admin_state.get(ADMIN_ID)
    if not state:
        return

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
        added = 0
        for line in lines:
            if "|" in line:
                name_part, ball_part = line.rsplit("|", 1)
                name = name_part.strip()
                ball_part = ball_part.strip().replace(",", ".")
                try:
                    ball = float(ball_part)
                except ValueError:
                    ball = None
            else:
                name = line.strip()
                ball = None
            if not name:
                continue
            programs[uuid.uuid4().hex[:8]] = {"name": name, "ball": ball}
            added += 1
        save_programs(programs)
        admin_state.pop(ADMIN_ID, None)
        await message.reply_text(
            f"✅ {added} ta yo'nalish qo'shildi. Jami: {len(programs)} ta.",
            reply_markup=admin_menu_keyboard(),
        )
        return

    if state == "awaiting_ad_text":
        new_text = (message.text or "").strip()
        global ad_text
        ad_text = new_text
        save_ad_text(new_text)
        admin_state.pop(ADMIN_ID, None)
        await message.reply_text("✅ Reklama matni saqlandi.", reply_markup=admin_menu_keyboard())
        return

    if state == "awaiting_import":
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


def main() -> None:
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("soni", count))
    app.add_handler(CallbackQueryHandler(check_subscription, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(toggle_subscription, pattern="^toggle_sub$"))
    app.add_handler(CallbackQueryHandler(start_calc, pattern="^start_calc$"))
    app.add_handler(CallbackQueryHandler(
        admin_callback,
        pattern="^(admin_|delch:|delprog:|confirm_broadcast|cancel_broadcast)"
    ))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.VIDEO) & ~filters.COMMAND, universal_input_handler))
    app.add_error_handler(global_error_handler)

    logger.info("Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
