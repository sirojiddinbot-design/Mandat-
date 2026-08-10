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

# Majburiy obuna kanallari. Railway'da FORCE_CHANNELS ga
# vergul bilan ajratib yozing, masalan:
# @kanal1,@kanal2,@kanal3
RAW_CHANNELS = os.environ.get("FORCE_CHANNELS", "")
FORCE_CHANNELS = [c.strip() for c in RAW_CHANNELS.split(",") if c.strip()]

DATA_FILE = Path(__file__).parent / "subscribers.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def load_subscribers() -> set[int]:
    if DATA_FILE.exists():
        return set(json.loads(DATA_FILE.read_text()))
    return set()


def save_subscribers(subs: set[int]) -> None:
    DATA_FILE.write_text(json.dumps(list(subs)))


subscribers: set[int] = load_subscribers()


# ---------------------------------------------------------
# Majburiy obunani tekshirish
# ---------------------------------------------------------
async def get_missing_channels(bot, user_id: int) -> list[str]:
    missing = []
    for channel in FORCE_CHANNELS:
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

    if FORCE_CHANNELS:
        missing = await get_missing_channels(context.bot, chat_id)
        if missing:
            await update.message.reply_text(
                "⚠️ Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling, "
                "so'ng «✅ Tekshirdim» tugmasini bosing:",
                reply_markup=build_subscription_keyboard(missing),
            )
            return

    await show_main_menu(chat_id, context.bot)


# ---------------------------------------------------------
# "✅ Tekshirdim" tugmasi
# ---------------------------------------------------------
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


# ---------------------------------------------------------
# Eslatma tugmasi
# ---------------------------------------------------------
async def toggle_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id

    if FORCE_CHANNELS:
        missing = await get_missing_channels(context.bot, chat_id)
        if missing:
            await query.answer("Avval kanallarga obuna bo'ling ❌", show_alert=True)
            await query.edit_message_reply_markup(reply_markup=build_subscription_keyboard(missing))
            return

    if chat_id in subscribers:
        subscribers.remove(chat_id)
        save_subscribers(subscribers)
        await query.answer("Eslatma o'chirildi")
        new_text = "🔔 Eslatmani yoqish"
        msg = "🔕 Siz obunani bekor qildingiz. Xohlasangiz, qayta yoqishingiz mumkin."
    else:
        subscribers.add(chat_id)
        save_subscribers(subscribers)
        await query.answer("Eslatma yoqildi ✅")
        new_text = "🔕 Eslatmani o'chirish"
        msg = "✅ Siz muvaffaqiyatli obuna bo'ldingiz! Natijalar e'lon qilinishi bilan sizga xabar boradi."

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(new_text, callback_data="toggle_sub")]]
    )
    await query.edit_message_text(msg, reply_markup=keyboard)


# ---------------------------------------------------------
# /xabar <matn> — faqat matnli xabar
# ---------------------------------------------------------
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Bu buyruq faqat admin uchun.")
        return

    text = update.message.text.replace("/xabar", "", 1).strip()
    if not text:
        await update.message.reply_text(
            "Foydalanish:\n"
            "• Matn: /xabar Sizning xabar matningiz\n"
            "• Rasm/video: rasm yoki videoni tanlang, izohiga (caption) "
            "/xabar bilan boshlab matn yozing va yuboring"
        )
        return

    await _send_to_all(context.bot, text=text)
    await update.message.reply_text("✅ Matnli xabar yuborildi.")


# ---------------------------------------------------------
# Rasm yoki video + /xabar izoh bilan yuborilsa
# ---------------------------------------------------------
async def broadcast_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    caption = update.message.caption or ""
    if not caption.startswith("/xabar"):
        return

    text = caption.replace("/xabar", "", 1).strip()
    photo_id = update.message.photo[-1].file_id if update.message.photo else None
    video_id = update.message.video.file_id if update.message.video else None

    await _send_to_all(context.bot, text=text, photo_id=photo_id, video_id=video_id)
    await update.message.reply_text("✅ Media xabar yuborildi.")


async def _send_to_all(
    bot, text: str = "", photo_id: str | None = None, video_id: str | None = None
) -> None:
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
            # Foydalanuvchi botni bloklagan — ro'yxatdan olib tashlaymiz
            subscribers.discard(chat_id)
            save_subscribers(subscribers)
            failed += 1
        except Exception as e:
            logger.warning(f"Xabar yuborilmadi {chat_id}: {e}")
            failed += 1

    logger.info(f"Broadcast: yuborildi={sent}, xato={failed}")


# ---------------------------------------------------------
# /soni
# ---------------------------------------------------------
async def count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(f"Hozirgi obunachilar soni: {len(subscribers)}")


def main() -> None:
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("xabar", broadcast))
    app.add_handler(CommandHandler("soni", count))
    app.add_handler(CallbackQueryHandler(check_subscription, pattern="check_sub"))
    app.add_handler(CallbackQueryHandler(toggle_subscription, pattern="toggle_sub"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, broadcast_media))

    logger.info("Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
