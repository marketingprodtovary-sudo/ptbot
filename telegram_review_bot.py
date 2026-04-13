"""
Telegram Bot для сбора отзывов — ПродТовары v3.0

Что нового:
- Убраны кнопки "Я подписался" / "Без подписки"
  Скидка через Instagram-сервис приходит автоматически в директ
- Кнопка "Ответить клиенту" у администраторов
- Приём отзывов с сайта через HTTP (порт 8080)
- Два администратора получают все уведомления

Переменные окружения:
    BOT_TOKEN   — токен бота от @BotFather
    PORT        — порт для приёма веб-отзывов (по умолчанию 8080)
"""

import os
import json
import logging
import hashlib
import random
import asyncio
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, ConversationHandler, filters
)
from telegram.constants import ParseMode

# ══════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "8326333850:AAHxoZudQiV0BYc7H9bt-uZJzVZwp9zHuuY")

ADMIN_IDS = [8196996608, 1689088225]

INSTAGRAM_LINK       = "https://www.instagram.com/ptbrest/"
COUPON_DISCOUNT      = 5        # % скидки на продукты собственного производства
COUPON_PREFIX        = "PT"
COUPON_VALIDITY_DAYS = 30

STORES_JSON_PATH = os.path.join(os.path.dirname(__file__), "stores.json")
PORT = int(os.getenv("PORT", 8080))
# ══════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Состояния диалога ──
SELECTING_SHOP, ENTERING_RATING, ENTERING_REVIEW, \
ENTERING_NAME, ENTERING_PHONE, CONFIRM_REVIEW = range(6)

# ── Хранилища ──
reviews_storage   = []
coupons_storage   = []
admin_reply_state = {}  # admin_id → {"user_id": int, "source": str, "email": str|None}

# Глобальная ссылка на приложение (для HTTP-сервера)
_app: Application = None
_bot_loop: asyncio.AbstractEventLoop = None


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ══════════════════════════════════════════════════════════
#  КЛАССЫ ДАННЫХ
# ══════════════════════════════════════════════════════════

class ReviewData:
    def __init__(self):
        self.shop_id     = None
        self.shop_name   = None
        self.branch      = None
        self.city        = None
        self.rating      = None
        self.review_text = None
        self.user_name   = None
        self.user_phone  = None
        self.user_email  = None
        self.user_tg_id  = None
        self.source      = "telegram"  # "telegram" | "website"
        self.timestamp   = datetime.now()


class CouponData:
    def __init__(self, code: str, discount: int, user_id: int):
        self.code       = code
        self.discount   = discount
        self.user_id    = user_id
        self.created_at = datetime.now()
        self.used       = False


# ══════════════════════════════════════════════════════════
#  МАГАЗИНЫ
# ══════════════════════════════════════════════════════════

def load_stores() -> dict:
    try:
        with open(STORES_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {s["id"]: s for s in data.get("stores", [])}
    except Exception as e:
        logger.error(f"Не удалось загрузить stores.json: {e}")
        return {
            "prodtovary-1": {"id": "prodtovary-1", "name": "Продтовары",
                             "branch": "ул. Советская, 45", "city": "г. Брест",
                             "brand": "prodtovary"},
        }

STORES = load_stores()
BRAND_ORDER = ["prodtovary", "diskaunter", "miks", "doma-zhdut", "podari-vino"]
BRAND_NAMES = {
    "prodtovary":  "Продтовары",
    "diskaunter":  "Дискаунтер",
    "miks":        "Супермаркет Микс",
    "doma-zhdut":  "Дома ждут!",
    "podari-vino": "Подари вино",
}


# ══════════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════════

def shop_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    by_brand = {}
    for s in STORES.values():
        by_brand.setdefault(s["brand"], []).append(s)
    for brand in BRAND_ORDER:
        shops = by_brand.get(brand, [])
        if not shops:
            continue
        buttons.append([InlineKeyboardButton(
            f"── {BRAND_NAMES.get(brand, brand)} ──", callback_data="noop"
        )])
        row = []
        for s in shops:
            row.append(InlineKeyboardButton(s["branch"], callback_data=f"shop_{s['id']}"))
            if len(row) == 2:
                buttons.append(row); row = []
        if row:
            buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def rating_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⭐ 1",      callback_data="rating_1"),
        InlineKeyboardButton("⭐⭐ 2",    callback_data="rating_2"),
        InlineKeyboardButton("⭐⭐⭐ 3",  callback_data="rating_3"),
    ],[
        InlineKeyboardButton("⭐⭐⭐⭐ 4",   callback_data="rating_4"),
        InlineKeyboardButton("⭐⭐⭐⭐⭐ 5", callback_data="rating_5"),
    ]])


# ══════════════════════════════════════════════════════════
#  HANDLERS — ПОЛЬЗОВАТЕЛЬ
# ══════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["review"] = ReviewData()
    review: ReviewData = context.user_data["review"]
    review.user_tg_id = update.effective_user.id

    args = context.args or []
    shop_id = None
    if args and args[0].startswith("review_"):
        candidate = args[0][7:]
        if candidate in STORES:
            shop_id = candidate

    if shop_id:
        s = STORES[shop_id]
        review.shop_id   = s["id"]
        review.shop_name = s["name"]
        review.branch    = s["branch"]
        review.city      = s.get("city", "")
        await update.message.reply_text(
            f"🎉 <b>Добро пожаловать!</b>\n\n"
            f"Вы выбрали магазин:\n"
            f"<b>{s['name']}</b> — {s['branch']}, {s.get('city','')}\n\n"
            f"Оцените ваш визит:",
            reply_markup=rating_keyboard(), parse_mode=ParseMode.HTML
        )
        return ENTERING_RATING

    await update.message.reply_text(
        "👋 <b>Здравствуйте!</b>\n\nВыберите магазин, в котором были:",
        reply_markup=shop_keyboard(), parse_mode=ParseMode.HTML
    )
    return SELECTING_SHOP


async def select_shop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "noop":
        return SELECTING_SHOP
    shop_id = query.data.replace("shop_", "")
    if shop_id not in STORES:
        return SELECTING_SHOP
    s = STORES[shop_id]
    review: ReviewData = context.user_data["review"]
    review.shop_id   = s["id"]
    review.shop_name = s["name"]
    review.branch    = s["branch"]
    review.city      = s.get("city", "")
    await query.edit_message_text(
        f"✅ Выбрано: <b>{s['name']}</b> — {s['branch']}\n\nОцените ваш визит:",
        reply_markup=rating_keyboard(), parse_mode=ParseMode.HTML
    )
    return ENTERING_RATING


async def select_rating(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    rating = int(query.data.split("_")[1])
    context.user_data["review"].rating = rating
    await query.edit_message_text(
        f"✅ Ваша оценка: {'⭐' * rating}\n\n"
        "Напишите подробный отзыв:\n"
        "<i>Что понравилось / не понравилось? (10–500 символов)</i>",
        parse_mode=ParseMode.HTML
    )
    return ENTERING_REVIEW


async def enter_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if is_admin(update.effective_user.id) and update.effective_user.id in admin_reply_state:
        return await handle_admin_reply_text(update, context)

    text = update.message.text.strip()
    if len(text) < 10:
        await update.message.reply_text("⚠️ Слишком короткий отзыв! Напишите минимум 10 символов.")
        return ENTERING_REVIEW
    if len(text) > 500:
        await update.message.reply_text("⚠️ Слишком длинный отзыв! Максимум 500 символов.")
        return ENTERING_REVIEW
    context.user_data["review"].review_text = text
    await update.message.reply_text(
        "😊 Спасибо! Как вас зовут? (необязательно)",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("Пропустить")]], resize_keyboard=True, one_time_keyboard=True
        )
    )
    return ENTERING_NAME


async def enter_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["review"].user_name = None if text == "Пропустить" else text
    await update.message.reply_text(
        "📱 Укажите номер телефона (необязательно):",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("Пропустить")]], resize_keyboard=True, one_time_keyboard=True
        )
    )
    return ENTERING_PHONE


async def enter_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text != "Пропустить":
        context.user_data["review"].user_phone = text
    review: ReviewData = context.user_data["review"]
    stars = "⭐" * review.rating
    preview = (
        f"🏪 <b>{review.shop_name}</b> — {review.branch}\n"
        f"⭐ Оценка: {stars}\n"
        f"👤 Имя: {review.user_name or 'Аноним'}\n"
        f"📱 Телефон: {review.user_phone or 'не указан'}\n\n"
        f"📝 <b>Отзыв:</b>\n{review.review_text}"
    )
    await update.message.reply_text(
        "📋 <b>Проверьте ваш отзыв:</b>\n\n" + preview,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Отправить", callback_data="confirm_yes"),
            InlineKeyboardButton("✏️ Изменить",  callback_data="confirm_no")
        ]]),
        parse_mode=ParseMode.HTML
    )
    return CONFIRM_REVIEW


async def confirm_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if "no" in query.data:
        context.user_data["review"] = ReviewData()
        await query.edit_message_text("🔄 Начинаем заново. Выберите магазин:", reply_markup=shop_keyboard())
        return SELECTING_SHOP

    review: ReviewData = context.user_data["review"]
    reviews_storage.append(review)
    await send_admin_notification(context.bot, review)

    # Финальное сообщение — скидка через Instagram-сервис, без лишних кнопок
    await query.edit_message_text(
        f"🎉 <b>Спасибо за отзыв! Ваше мнение важно для нас 💙</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎁 <b>Ваш бонус — скидка {COUPON_DISCOUNT}%</b>\n\n"
        f"Подпишитесь на наш Instagram <b>@ptbrest</b> — "
        f"сервис автоматически пришлёт вам слово-пароль в директ.\n"
        f"Покажите его кассиру и получите скидку {COUPON_DISCOUNT}% "
        f"на продукты нашего собственного производства 🛒\n\n"
        f"📸 <a href='{INSTAGRAM_LINK}'>Перейти в Instagram @ptbrest</a>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if is_admin(update.effective_user.id) and update.effective_user.id in admin_reply_state:
        del admin_reply_state[update.effective_user.id]
        await update.message.reply_text("❌ Режим ответа отменён.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    await update.message.reply_text("❌ Отменено. /start — начать заново.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ℹ️ <b>Справка</b>\n\n"
        "/start — оставить отзыв\n"
        "/cancel — отменить\n",
        parse_mode=ParseMode.HTML
    )


# ══════════════════════════════════════════════════════════
#  УВЕДОМЛЕНИЕ АДМИНИСТРАТОРАМ
# ══════════════════════════════════════════════════════════

async def send_admin_notification(bot, review: ReviewData) -> None:
    stars = "⭐" * (review.rating or 0)
    source_label = "🌐 Сайт" if review.source == "website" else "✈️ Telegram"

    msg = (
        f"📬 <b>НОВЫЙ ОТЗЫВ</b> [{source_label}]\n\n"
        f"🏪 <b>Магазин:</b> {review.shop_name} — {review.branch}, {review.city or ''}\n"
        f"⭐ <b>Рейтинг:</b> {stars} ({review.rating}/5)\n"
        f"👤 <b>Имя:</b> {review.user_name or 'Аноним'}\n"
        f"📱 <b>Телефон:</b> {review.user_phone or 'не указан'}\n"
    )
    if review.user_email:
        msg += f"📧 <b>Email:</b> {review.user_email}\n"
    if review.user_tg_id:
        msg += f"🤖 <b>Telegram ID:</b> {review.user_tg_id}\n"
    msg += (
        f"⏰ <b>Время:</b> {review.timestamp.strftime('%d.%m.%Y %H:%M')}\n\n"
        f"📝 <b>Текст отзыва:</b>\n{review.review_text}"
    )

    # Кнопка ответа
    keyboard = None
    if review.source == "telegram" and review.user_tg_id:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("💬 Ответить клиенту", callback_data=f"reply_tg_{review.user_tg_id}")
        ]])
    elif review.source == "website":
        contact = review.user_email or review.user_phone or ""
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "📋 Показать контакт клиента",
                callback_data=f"reply_web_0_{contact[:50]}"
            )
        ]])

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id, text=msg,
                parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления {admin_id}: {e}")


# ══════════════════════════════════════════════════════════
#  ОТВЕТЫ КЛИЕНТАМ
# ══════════════════════════════════════════════════════════

async def admin_reply_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Нет прав.", show_alert=True)
        return

    data = query.data

    if data.startswith("reply_tg_"):
        user_id = int(data.replace("reply_tg_", ""))
        admin_reply_state[query.from_user.id] = {"user_id": user_id, "source": "tg"}
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=(
                f"💬 <b>Режим ответа клиенту (Telegram)</b>\n\n"
                f"Напишите сообщение — оно придёт клиенту прямо в этот бот.\n\n"
                f"Для отмены: /cancel или нажмите «❌ Отмена»"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("❌ Отмена")]], resize_keyboard=True, one_time_keyboard=True
            )
        )

    elif data.startswith("reply_web_"):
        # Показываем контакт клиента с сайта
        contact = data.replace("reply_web_0_", "")
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=(
                f"📋 <b>Контакт клиента с сайта:</b>\n\n"
                f"<code>{contact or 'не указан'}</code>\n\n"
                f"Напишите ему на email или позвоните по телефону.\n"
                f"Автоматический ответ через бота для клиентов без Telegram недоступен."
            ),
            parse_mode=ParseMode.HTML
        )


async def handle_admin_reply_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id

    if update.message.text == "❌ Отмена":
        del admin_reply_state[admin_id]
        await update.message.reply_text("❌ Режим ответа отменён.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    state = admin_reply_state.get(admin_id)
    if not state:
        return

    reply_text = update.message.text.strip()

    try:
        await context.bot.send_message(
            chat_id=state["user_id"],
            text=(
                f"📩 <b>Ответ от магазина ПродТовары:</b>\n\n"
                f"{reply_text}\n\n"
                f"<i>Если есть вопросы — напишите нам снова через /start</i>"
            ),
            parse_mode=ParseMode.HTML
        )
        await update.message.reply_text(
            "✅ <b>Ответ отправлен клиенту!</b>",
            parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove()
        )
        for other_id in ADMIN_IDS:
            if other_id != admin_id:
                try:
                    await context.bot.send_message(
                        chat_id=other_id,
                        text=f"ℹ️ Коллега ответил клиенту:\n\n{reply_text}"
                    )
                except Exception:
                    pass
    except Exception as e:
        await update.message.reply_text(
            f"❌ Не удалось отправить. Возможно, клиент заблокировал бота.\n{e}",
            reply_markup=ReplyKeyboardRemove()
        )

    del admin_reply_state[admin_id]
    return ConversationHandler.END


async def admin_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if update.effective_user.id in admin_reply_state:
        await handle_admin_reply_text(update, context)


# ══════════════════════════════════════════════════════════
#  ADMIN-КОМАНДЫ
# ══════════════════════════════════════════════════════════

async def check_coupon_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /check <код_купона>"); return
    result = {"valid": False, "message": "❌ Купон не найден"}
    code = context.args[0].upper()
    for c in coupons_storage:
        if c.code == code:
            result = {"message": f"✅ Купон действителен! Скидка {c.discount}%"} if not c.used else {"message": "❌ Уже использован"}
            break
    await update.message.reply_text(result["message"])


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    total     = len(reviews_storage)
    avg       = (sum(r.rating for r in reviews_storage if r.rating) / total) if total else 0
    tg_count  = sum(1 for r in reviews_storage if r.source == "telegram")
    web_count = sum(1 for r in reviews_storage if r.source == "website")
    shops = {}
    for r in reviews_storage:
        shops[r.shop_name] = shops.get(r.shop_name, 0) + 1
    top = "\n".join(f"  • {k}: {v}" for k, v in sorted(shops.items(), key=lambda x: -x[1])[:5]) or "  нет данных"
    await update.message.reply_text(
        f"📊 <b>Статистика отзывов</b>\n\n"
        f"Всего: <b>{total}</b>\n"
        f"  — Telegram: <b>{tg_count}</b>\n"
        f"  — Сайт: <b>{web_count}</b>\n"
        f"Средняя оценка: <b>{avg:.1f} ⭐</b>\n\n"
        f"<b>Топ магазины:</b>\n{top}",
        parse_mode=ParseMode.HTML
    )


# ══════════════════════════════════════════════════════════
#  HTTP-СЕРВЕР ДЛЯ ПРИЁМА ОТЗЫВОВ С САЙТА
# ══════════════════════════════════════════════════════════

class WebhookHandler(BaseHTTPRequestHandler):
    """Принимает POST /webhook/web-review от сайта Битрикс."""

    def log_message(self, format, *args):
        logger.info(f"HTTP {self.command} {self.path}: {format % args}")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok","service":"ProdTovary Review Bot"}')

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        if self.path != "/webhook/web-review":
            self.send_response(404)
            self.end_headers()
            return

        try:
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            data = json.loads(body.decode("utf-8"))
        except Exception as e:
            logger.error(f"Ошибка разбора JSON: {e}")
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"invalid json"}')
            return

        review = ReviewData()
        review.source     = "website"
        review.shop_id    = data.get("shop_id", "")
        review.shop_name  = data.get("shop_name", "Магазин")
        review.branch     = data.get("branch", "")
        review.city       = data.get("city", "")
        review.rating     = int(data.get("rating", 0))
        review.review_text= str(data.get("review", ""))
        review.user_name  = data.get("name") or None
        review.user_phone = data.get("phone") or None
        review.user_email = data.get("email") or None
        review.user_tg_id = None
        review.timestamp  = datetime.now()
        reviews_storage.append(review)

        # Отправляем уведомление через event loop бота
        if _bot_loop and _app:
            asyncio.run_coroutine_threadsafe(
                send_admin_notification(_app.bot, review),
                _bot_loop
            )

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b'{"status":"ok","message":"review received"}')


def run_http_server():
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    logger.info(f"HTTP-сервер для приёма отзывов с сайта запущен на порту {PORT}")
    server.serve_forever()


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════

def main():
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())  # ← добавьте эту строку
def main() -> None:
    global _app, _bot_loop

    application = Application.builder().token(BOT_TOKEN).build()
    _app = application

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECTING_SHOP:  [CallbackQueryHandler(select_shop,   pattern=r"^(shop_|noop)")],
            ENTERING_RATING: [CallbackQueryHandler(select_rating, pattern=r"^rating_")],
            ENTERING_REVIEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_review)],
            ENTERING_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_name)],
            ENTERING_PHONE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_phone)],
            CONFIRM_REVIEW:  [CallbackQueryHandler(confirm_review, pattern=r"^confirm_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help",  help_command))
    application.add_handler(CommandHandler("check", check_coupon_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CallbackQueryHandler(admin_reply_button, pattern=r"^reply_(tg|web)_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_message_handler))

    # Запускаем HTTP-сервер в фоне
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()

    # Сохраняем loop после старта
    async def _save_loop(app):
        global _bot_loop
        _bot_loop = asyncio.get_event_loop()

    application.post_init = _save_loop

    logger.info("Бот запущен. Администраторы: %s", ADMIN_IDS)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
