"""
Telegram Bot для сбора отзывов — ПродТовары
Версия 2.0: несколько админов + ответы клиентам прямо из бота.

Установка:
    pip install python-telegram-bot==21.9

Запуск:
    python telegram_review_bot.py

Переменные окружения (опционально):
    BOT_TOKEN      — токен бота от @BotFather
"""

import os
import json
import logging
from datetime import datetime, timedelta
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
BOT_TOKEN = os.getenv("BOT_TOKEN", "8326333850:AAFAG5vaWAjcr25gKfhj4C57qd0zf3Dbki4")

# Список всех администраторов (получают уведомления и могут отвечать клиентам)
ADMIN_IDS = [8196996608, 1689088225]

INSTAGRAM_LINK       = "https://www.instagram.com/ptbrest/"
COUPON_DISCOUNT      = 10       # % скидки
COUPON_PREFIX        = "PT"     # префикс кода купона
COUPON_VALIDITY_DAYS = 30       # срок действия купона

STORES_JSON_PATH = os.path.join(os.path.dirname(__file__), "stores.json")
# ══════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Состояния диалога ──
SELECTING_SHOP, ENTERING_RATING, ENTERING_REVIEW, \
ENTERING_NAME, ENTERING_PHONE, CONFIRM_REVIEW, INSTAGRAM_OFFER = range(7)

# ── Хранилища (в памяти; при перезапуске сбрасываются) ──
reviews_storage = []
coupons_storage = []

# Словарь: admin_id → user_id, которому админ сейчас пишет ответ
admin_reply_state = {}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ══════════════════════════════════════════════════════════
#  КЛАССЫ ДАННЫХ
# ══════════════════════════════════════════════════════════

class ReviewData:
    """Хранит данные одного отзыва."""
    def __init__(self):
        self.shop_id    = None
        self.shop_name  = None
        self.branch     = None
        self.city       = None
        self.rating     = None
        self.review_text= None
        self.user_name  = None
        self.user_phone = None
        self.user_tg_id = None
        self.timestamp  = datetime.now()


class CouponData:
    """Хранит данные купона."""
    def __init__(self, code: str, discount: int, user_id: int):
        self.code       = code
        self.discount   = discount
        self.user_id    = user_id
        self.created_at = datetime.now()
        self.used       = False
        self.used_at    = None


# ══════════════════════════════════════════════════════════
#  ЗАГРУЗКА МАГАЗИНОВ ИЗ JSON
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
#  КУПОНЫ
# ══════════════════════════════════════════════════════════

def generate_coupon(user_id: int) -> str:
    import hashlib, random
    base = f"{user_id}{datetime.now().timestamp()}{random.randint(1000, 9999)}"
    code = f"{COUPON_PREFIX}{hashlib.md5(base.encode()).hexdigest()[:6].upper()}"
    coupons_storage.append(CouponData(code, COUPON_DISCOUNT, user_id))
    return code


def verify_coupon(code: str) -> dict:
    for c in coupons_storage:
        if c.code == code:
            if c.used:
                return {"valid": False, "message": "❌ Купон уже использован"}
            if (datetime.now() - c.created_at).days > COUPON_VALIDITY_DAYS:
                return {"valid": False, "message": f"❌ Купон истёк (срок {COUPON_VALIDITY_DAYS} дней)"}
            return {"valid": True, "message": f"✅ Купон действителен! Скидка {c.discount}%",
                    "discount": c.discount}
    return {"valid": False, "message": "❌ Купон не найден"}


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
            f"── {BRAND_NAMES.get(brand, brand)} ──",
            callback_data="noop"
        )])
        row = []
        for i, s in enumerate(shops):
            row.append(InlineKeyboardButton(s["branch"], callback_data=f"shop_{s['id']}"))
            if len(row) == 2:
                buttons.append(row); row = []
        if row:
            buttons.append(row)

    return InlineKeyboardMarkup(buttons)


def rating_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⭐ 1", callback_data="rating_1"),
        InlineKeyboardButton("⭐⭐ 2", callback_data="rating_2"),
        InlineKeyboardButton("⭐⭐⭐ 3", callback_data="rating_3"),
    ],[
        InlineKeyboardButton("⭐⭐⭐⭐ 4", callback_data="rating_4"),
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

        welcome = (
            f"🎉 <b>Добро пожаловать!</b>\n\n"
            f"Вы выбрали магазин:\n"
            f"<b>{s['name']}</b> — {s['branch']}, {s.get('city','')}\n\n"
            f"Оцените ваш визит:"
        )
        msg = update.message or (update.callback_query and update.callback_query.message)
        await msg.reply_text(welcome, reply_markup=rating_keyboard(), parse_mode=ParseMode.HTML)
        return ENTERING_RATING

    await (update.message or update.callback_query.message).reply_text(
        "👋 <b>Здравствуйте!</b>\n\nВыберите магазин, в котором были:",
        reply_markup=shop_keyboard(),
        parse_mode=ParseMode.HTML
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
        reply_markup=rating_keyboard(),
        parse_mode=ParseMode.HTML
    )
    return ENTERING_RATING


async def select_rating(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    rating = int(query.data.split("_")[1])
    context.user_data["review"].rating = rating
    stars = "⭐" * rating

    await query.edit_message_text(
        f"✅ Ваша оценка: {stars}\n\n"
        "Напишите подробный отзыв:\n"
        "<i>Что понравилось / не понравилось? (10–500 символов)</i>",
        parse_mode=ParseMode.HTML
    )
    return ENTERING_REVIEW


async def enter_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Если это админ, который сейчас пишет ответ клиенту — перехватываем
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
        "📱 Можем ли мы с вами связаться? Укажите номер телефона (необязательно):",
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

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Отправить", callback_data="confirm_yes"),
        InlineKeyboardButton("✏️ Изменить",  callback_data="confirm_no")
    ]])

    await update.message.reply_text(
        "📋 <b>Проверьте ваш отзыв:</b>\n\n" + preview,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )
    return CONFIRM_REVIEW


async def confirm_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if "no" in query.data:
        context.user_data["review"] = ReviewData()
        await query.edit_message_text("🔄 Начинаем заново. Выберите магазин:",
                                       reply_markup=shop_keyboard())
        return SELECTING_SHOP

    review: ReviewData = context.user_data["review"]
    reviews_storage.append(review)

    coupon_code = generate_coupon(update.effective_user.id)
    context.user_data["coupon"] = coupon_code

    await send_admin_notification(context, review, coupon_code=coupon_code)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Подписаться на Instagram", url=INSTAGRAM_LINK)],
        [InlineKeyboardButton("✅ Я подписался!",           callback_data="instagram_yes")],
        [InlineKeyboardButton("Получить купон без подписки", callback_data="instagram_skip")],
    ])

    expire_date = (datetime.now() + timedelta(days=COUPON_VALIDITY_DAYS)).strftime("%d.%m.%Y")

    await query.edit_message_text(
        f"🎉 <b>Спасибо за отзыв! Ваше мнение важно для нас 💙</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎁 <b>Получите купон на скидку {COUPON_DISCOUNT}%!</b>\n\n"
        f"Подпишитесь на наш Instagram <b>@ptbrest</b>,\n"
        f"нажмите «Я подписался» и купон активируется.\n\n"
        f"<b>Ваш купон:</b> <code>{coupon_code}</code>\n"
        f"<b>Действителен до:</b> {expire_date}",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )
    return INSTAGRAM_OFFER


async def instagram_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("🎉 Отлично! Купон активирован!")

    coupon_code = context.user_data.get("coupon", "—")
    expire_date = (datetime.now() + timedelta(days=COUPON_VALIDITY_DAYS)).strftime("%d.%m.%Y")

    await query.edit_message_text(
        f"🎉 <b>КУПОН АКТИВИРОВАН!</b>\n\n"
        f"✅ Спасибо, что подписались на @ptbrest!\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>ВАШ КУПОН:</b>\n\n"
        f"<code>{coupon_code}</code>\n\n"
        f"<b>Скидка:</b> {COUPON_DISCOUNT}%\n"
        f"<b>Действителен до:</b> {expire_date}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Как использовать:</b>\n"
        f"1. Придите в любой наш магазин\n"
        f"2. Покажите код кассиру\n"
        f"3. Получите скидку {COUPON_DISCOUNT}%!\n\n"
        f"Следите за новостями: {INSTAGRAM_LINK}",
        parse_mode=ParseMode.HTML
    )

    review = context.user_data.get("review")
    if review:
        await send_admin_notification(context, review, coupon_code=coupon_code,
                                      instagram_subscribed=True)
    return ConversationHandler.END


async def instagram_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    coupon_code = context.user_data.get("coupon", "—")
    expire_date = (datetime.now() + timedelta(days=COUPON_VALIDITY_DAYS)).strftime("%d.%m.%Y")

    await query.edit_message_text(
        f"👋 Хорошо! Ваш купон всё равно действует:\n\n"
        f"<code>{coupon_code}</code>\n\n"
        f"<b>Скидка:</b> {COUPON_DISCOUNT}%\n"
        f"<b>Действителен до:</b> {expire_date}\n\n"
        f"Покажите код кассиру в любом нашем магазине.\n"
        f"Если передумаете подписаться: {INSTAGRAM_LINK}",
        parse_mode=ParseMode.HTML
    )

    review = context.user_data.get("review")
    if review:
        await send_admin_notification(context, review, coupon_code=coupon_code,
                                      instagram_subscribed=False)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Если админ отменяет режим ответа
    if is_admin(update.effective_user.id) and update.effective_user.id in admin_reply_state:
        del admin_reply_state[update.effective_user.id]
        await update.message.reply_text("❌ Режим ответа клиенту отменён.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    await update.message.reply_text(
        "❌ Процесс отменён. Используйте /start для начала.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ℹ️ <b>Справка</b>\n\n"
        "Этот бот собирает отзывы о магазинах ПродТовары.\n\n"
        "/start — оставить отзыв\n"
        "/cancel — отменить\n",
        parse_mode=ParseMode.HTML
    )


# ══════════════════════════════════════════════════════════
#  УВЕДОМЛЕНИЕ АДМИНИСТРАТОРАМ
# ══════════════════════════════════════════════════════════

async def send_admin_notification(
    context: ContextTypes.DEFAULT_TYPE,
    review: ReviewData,
    coupon_code: str = None,
    instagram_subscribed: bool = None
) -> None:
    """Отправляет уведомление всем администраторам с кнопкой ответа клиенту."""
    try:
        stars = "⭐" * (review.rating or 0)

        # Имя пользователя в Telegram если есть
        username_line = ""
        if review.user_tg_id:
            username_line = f"🔗 <b>Написать напрямую:</b> tg://user?id={review.user_tg_id}\n"

        msg = (
            f"📬 <b>НОВЫЙ ОТЗЫВ</b>\n\n"
            f"🏪 <b>Магазин:</b> {review.shop_name} — {review.branch}, {review.city or ''}\n"
            f"⭐ <b>Рейтинг:</b> {stars} ({review.rating}/5)\n"
            f"👤 <b>Имя:</b> {review.user_name or 'Аноним'}\n"
            f"📱 <b>Телефон:</b> {review.user_phone or 'не указан'}\n"
            f"🤖 <b>Telegram ID:</b> {review.user_tg_id or '—'}\n"
            f"{username_line}"
            f"⏰ <b>Время:</b> {review.timestamp.strftime('%d.%m.%Y %H:%M')}\n\n"
            f"📝 <b>Текст отзыва:</b>\n{review.review_text}\n"
        )

        if coupon_code:
            expire = (datetime.now() + timedelta(days=COUPON_VALIDITY_DAYS)).strftime("%d.%m.%Y")
            msg += (
                f"\n━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎁 <b>Купон:</b> <code>{coupon_code}</code>\n"
                f"💰 Скидка: {COUPON_DISCOUNT}%\n"
                f"📅 Истекает: {expire}\n"
            )

        if instagram_subscribed is True:
            msg += "\n✅ <b>Instagram:</b> ПОДПИСАЛСЯ"
        elif instagram_subscribed is False:
            msg += "\n❌ <b>Instagram:</b> Пропустил"

        # Кнопка "Ответить клиенту" — только если есть Telegram ID
        keyboard = None
        if review.user_tg_id:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "💬 Ответить клиенту",
                    callback_data=f"reply_{review.user_tg_id}"
                )
            ]])

        # Отправляем всем администраторам
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=msg,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление администратору {admin_id}: {e}")

    except Exception as e:
        logger.error(f"Ошибка отправки уведомления: {e}")


# ══════════════════════════════════════════════════════════
#  ОТВЕТЫ КЛИЕНТАМ (для администраторов)
# ══════════════════════════════════════════════════════════

async def admin_reply_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажатие кнопки 'Ответить клиенту' — переводит админа в режим ответа."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("⛔ У вас нет прав администратора.", show_alert=True)
        return

    user_id = int(query.data.replace("reply_", ""))
    admin_reply_state[query.from_user.id] = user_id

    await context.bot.send_message(
        chat_id=query.from_user.id,
        text=(
            f"💬 <b>Режим ответа клиенту</b>\n\n"
            f"Напишите ваше сообщение — оно будет отправлено клиенту (ID: {user_id}).\n\n"
            f"Для отмены напишите /cancel"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("❌ Отмена")]], resize_keyboard=True, one_time_keyboard=True
        )
    )


async def handle_admin_reply_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текст, который админ пишет в режиме ответа."""
    admin_id = update.effective_user.id

    if update.message.text == "❌ Отмена":
        del admin_reply_state[admin_id]
        await update.message.reply_text("❌ Режим ответа отменён.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    user_id = admin_reply_state.get(admin_id)
    if not user_id:
        return

    reply_text = update.message.text.strip()

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"📩 <b>Ответ от магазина ПродТовары:</b>\n\n"
                f"{reply_text}\n\n"
                f"<i>Если у вас есть вопросы, напишите нам снова через /start</i>"
            ),
            parse_mode=ParseMode.HTML
        )

        # Подтверждение админу
        await update.message.reply_text(
            f"✅ <b>Ответ отправлен клиенту!</b>\n\n"
            f"<i>Ваше сообщение:</i>\n{reply_text}",
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove()
        )

        # Уведомить других админов
        for other_admin_id in ADMIN_IDS:
            if other_admin_id != admin_id:
                try:
                    await context.bot.send_message(
                        chat_id=other_admin_id,
                        text=(
                            f"ℹ️ <b>Коллега ответил клиенту</b> (ID: {user_id}):\n\n"
                            f"{reply_text}"
                        ),
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass

        del admin_reply_state[admin_id]

    except Exception as e:
        await update.message.reply_text(
            f"❌ Не удалось отправить сообщение клиенту.\n"
            f"Возможно, пользователь заблокировал бота.\n\n"
            f"Ошибка: {e}",
            reply_markup=ReplyKeyboardRemove()
        )
        del admin_reply_state[admin_id]

    return ConversationHandler.END


async def admin_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перехватывает сообщения от администраторов вне диалога (режим ответа клиенту)."""
    if not is_admin(update.effective_user.id):
        return
    if update.effective_user.id in admin_reply_state:
        await handle_admin_reply_text(update, context)


# ══════════════════════════════════════════════════════════
#  ADMIN: проверка купона и статистика
# ══════════════════════════════════════════════════════════

async def check_coupon_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /check <код_купона>")
        return
    result = verify_coupon(args[0].upper())
    await update.message.reply_text(result["message"])


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return

    total = len(reviews_storage)
    avg   = (sum(r.rating for r in reviews_storage if r.rating) / total) if total else 0
    shops = {}
    for r in reviews_storage:
        shops[r.shop_name] = shops.get(r.shop_name, 0) + 1

    top = "\n".join(f"  • {k}: {v}" for k, v in
                    sorted(shops.items(), key=lambda x: -x[1])[:5]) or "  нет данных"

    await update.message.reply_text(
        f"📊 <b>Статистика отзывов</b>\n\n"
        f"Всего отзывов: <b>{total}</b>\n"
        f"Средняя оценка: <b>{avg:.1f} ⭐</b>\n"
        f"Купонов выдано: <b>{len(coupons_storage)}</b>\n\n"
        f"<b>Топ магазины по отзывам:</b>\n{top}",
        parse_mode=ParseMode.HTML
    )


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECTING_SHOP: [
                CallbackQueryHandler(select_shop, pattern=r"^(shop_|noop)")
            ],
            ENTERING_RATING: [
                CallbackQueryHandler(select_rating, pattern=r"^rating_")
            ],
            ENTERING_REVIEW: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_review)
            ],
            ENTERING_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_name)
            ],
            ENTERING_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_phone)
            ],
            CONFIRM_REVIEW: [
                CallbackQueryHandler(confirm_review, pattern=r"^confirm_")
            ],
            INSTAGRAM_OFFER: [
                CallbackQueryHandler(instagram_yes,  pattern=r"^instagram_yes$"),
                CallbackQueryHandler(instagram_skip, pattern=r"^instagram_skip$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help",  help_command))
    application.add_handler(CommandHandler("check", check_coupon_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))

    # Кнопка "Ответить клиенту" от администратора
    application.add_handler(CallbackQueryHandler(admin_reply_button, pattern=r"^reply_\d+$"))

    # Сообщения от администраторов вне диалога (режим ответа)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        admin_message_handler
    ))

    logger.info("Бот запущен. Администраторы: %s", ADMIN_IDS)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
