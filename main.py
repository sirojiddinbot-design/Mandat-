import json
import logging
import os
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------
# SOZLAMALAR — bu qiymatlar Railway'dagi Environment Variables
# bo'limidan o'qiladi. Kodga tokenni yozmang!
# ---------------------------------------------------------
TOKEN = os.environ["TELEGRAM_TOKEN"]           # BotFather'dan olingan token
ADMIN_ID = int(os.environ["ADMIN_ID"])          # Sizning shaxsiy Telegram ID'ingiz

DATA_FILE = Path(__file__).parent / "subscribers.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# Obunachilarni saqlash / o'qish
# ---------------------------------------------------------
def load_subscribers() -> set[int]:
    if DATA_FILE.exists():
        return set(json.loads(DATA_FILE.read_text()))
    return set()


def save_subscribers(subs: set[int]) -> None:
    DATA_FILE.write_text(json.dumps(list(subs)))


subscribers: set[int] = load_subscribers()


# ---------------------------------------------------------
# /start — foydalanuvchiga tugma ko'rsatish
# ---------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    is_subscribed = chat_id in subscribers

    button_text = "🔕 Eslatmani o'chirish" if is_subscribed else "🔔 Eslatmani yoqish"
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(button_text, callback_data="toggle_sub")]]
    )

    await update.message.reply_text(
        "📣 <b>Mandat natijalari haqida xabardor bo'lish</b>\n\n"
        "Yakuniy natijalar e'lon qilinganda tezkor xabar olish uchun "
        "quyidagi tugmani bosing.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# ---------------------------------------------------------
# Tugma bosilganda — obuna holatini almashtirish
# ---------------------------------------------------------
async def toggle_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id

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
# /xabar — FAQAT ADMIN uchun. Barcha obunachilarga xabar yuboradi.
# Foydalanish: /xabar Natijalar e'lon qilindi! Saytga kiring.
# ---------------------------------------------------------
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Bu buyruq faqat admin uchun.")
        return

    text = update.message.text.replace("/xabar", "", 1).strip()
    if not text:
        await update.message.reply_text("Foydalanish: /xabar Sizning xabar matningiz")
        return

    sent, failed = 0, 0
    for chat_id in list(subscribers):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
            sent += 1
        except Exception as e:
            logger.warning(f"Xabar yuborilmadi {chat_id}: {e}")
            failed += 1

    await update.message.reply_text(
        f"✅ Xabar yuborildi.\nMuvaffaqiyatli: {sent}\nXato: {failed}"
    )


# ---------------------------------------------------------
# /soni — obunachilar sonini ko'rish (admin uchun)
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
    app.add_handler(CallbackQueryHandler(toggle_subscription, pattern="toggle_sub"))

    logger.info("Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
