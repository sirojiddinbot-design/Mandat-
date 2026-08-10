import json
import logging
import os
from pathlib import Path

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

# Boshlang'ich kanallar (ixtiyoriy) — keyinchalik admin panel orqali
# kanal qo'shish/o'chirish mumkin, Railway'ga qayta kirish shart emas.
SEED_CHANNELS = os.environ.get("FORCE_CHANNELS", "")

BASE_DIR = Path(__file__).parent
SUBS_FILE = BASE_DIR / "subscribers.json"
CHANNELS_FILE = BASE_DIR / "channels.json"

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
        [[InlineKeyboardButton(button_text, callback_data="toggle_sub")]]
    )
    text = (
        "📣 <b>Mandat natijalari haqida xabardor bo'lish</b>\n\n"
        "Yakuniy natijalar e'lon qilinganda tezkor xabar olish uchun "
        "quyidagi tugmani bosing."
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
# Adminning matn/media xabarlarini qabul qilish (holatga qarab)
# ---------------------------------------------------------
async def admin_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    state = admin_state.get(ADMIN_ID)
    if not state:
        return  # Admin oddiy foydalanuvchi kabi yozayotgan bo'lishi mumkin

    message = update.message

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
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^(admin_|delch:|confirm_broadcast|cancel_broadcast)"))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.VIDEO) & ~filters.COMMAND, admin_input_handler))

    logger.info("Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
