"""
ПродТовары — Бот обратной связи v4.0

Принцип: минимум трений.
  Deep-link с сайта → сразу пиши сообщение (1 касание)
  Без deep-link → выбери магазин → пиши (2 касания)

Не собираем имя/телефон — отвечаем в том же канале.
Звёзды убраны — важен текст.

Переменные окружения:
    BOT_TOKEN   — токен от @BotFather
"""

import os
import json
import logging
import asyncio
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
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

# Маршрутизация: brand → ID ответственного сотрудника
# Раскомментируйте и добавьте ID когда узнаете их через @userinfobot
STORE_STAFF: dict = {
    # "prodtovary":  123456789,
    # "diskaunter":  987654321,
}

STORES_JSON_PATH = os.path.join(os.path.dirname(__file__), "stores.json")
PORT = int(os.getenv("PORT", 8080))
# ══════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s %(levelname)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

SELECTING_SHOP, WRITING_MESSAGE = range(2)

reply_mode: dict = {}   # admin_id → {user_id, source}
_app: Application = None
_bot_loop = None


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ── Загрузка магазинов ──────────────────────────────────

def load_stores() -> dict:
    try:
        with open(STORES_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {s["id"]: s for s in data.get("stores", [])}
    except Exception as e:
        logger.error(f"stores.json: {e}")
        return {
            "prodtovary-2": {
                "id": "prodtovary-2", "name": "Продтовары",
                "branch": "ул. Волгоградская, 1", "city": "г. Брест",
                "brand": "prodtovary"
            },
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


def shop_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    by_brand: dict = {}
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
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
    return InlineKeyboardMarkup(buttons)


# ── Клиентский диалог ───────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    ctx.user_data.clear()
    ctx.user_data["tg_id"] = user.id
    ctx.user_data["username"] = (
        f"@{user.username}" if user.username else user.full_name or str(user.id)
    )

    args = ctx.args or []
    if args and args[0].startswith("review_"):
        shop_id = args[0][7:]
        if shop_id in STORES:
            s = STORES[shop_id]
            ctx.user_data["shop"] = s
            await update.message.reply_text(
                f"✍️ <b>{s['name']}</b> — {s['branch']}\n\n"
                f"Напишите — что понравилось, что нет, или любой вопрос.\n"
                f"Мы ответим здесь.",
                parse_mode=ParseMode.HTML,
                reply_markup=ReplyKeyboardRemove()
            )
            return WRITING_MESSAGE

    await update.message.reply_text(
        "Выберите магазин:",
        reply_markup=shop_keyboard()
    )
    return SELECTING_SHOP


async def cb_select_shop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "noop":
        return SELECTING_SHOP
    shop_id = query.data.replace("shop_", "")
    if shop_id not in STORES:
        return SELECTING_SHOP
    s = STORES[shop_id]
    ctx.user_data["shop"] = s
    await query.edit_message_text(
        f"✍️ <b>{s['name']}</b> — {s['branch']}\n\n"
        f"Напишите — что понравилось, что нет, или любой вопрос.\n"
        f"Мы ответим здесь.",
        parse_mode=ParseMode.HTML
    )
    return WRITING_MESSAGE


async def receive_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id

    # Администратор в режиме ответа — перехватываем
    if is_admin(uid) and uid in reply_mode:
        return await _admin_send_reply(update, ctx)

    text = update.message.text.strip()
    if len(text) < 3:
        await update.message.reply_text("Напишите чуть подробнее.")
        return WRITING_MESSAGE

    shop     = ctx.user_data.get("shop", {})
    tg_id    = ctx.user_data.get("tg_id", uid)
    username = ctx.user_data.get("username", str(uid))

    await update.message.reply_text(
        "✅ Сообщение отправлено! Ответим вам здесь в Telegram.",
        reply_markup=ReplyKeyboardRemove()
    )

    await _notify_admins(ctx.bot, shop, text, tg_id, username, source="tg")
    ctx.user_data.clear()
    return ConversationHandler.END


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    if uid in reply_mode:
        del reply_mode[uid]
        await update.message.reply_text("Режим ответа отменён.")
        return ConversationHandler.END
    await update.message.reply_text("Отменено. /start — начать заново.")
    return ConversationHandler.END


# ── Уведомление администраторов ─────────────────────────

async def _notify_admins(bot, shop: dict, text: str,
                         tg_id: int, username: str,
                         source: str = "tg") -> None:
    shop_name   = shop.get("name", "—")
    shop_branch = shop.get("branch", "—")
    shop_brand  = shop.get("brand", "")
    ts = datetime.now().strftime("%d.%m %H:%M")
    icon = "✈️" if source == "tg" else "🌐"

    user_line = username
    if source == "tg" and tg_id:
        user_line += f" · <a href='tg://user?id={tg_id}'>написать напрямую</a>"

    msg = (
        f"📩 <b>Новое сообщение</b> {icon}\n"
        f"🏪 {shop_name} — {shop_branch}\n"
        f"👤 {user_line}\n"
        f"⏱ {ts}\n\n"
        f"{text}"
    )

    buttons = []
    if source == "tg" and tg_id:
        buttons.append([InlineKeyboardButton(
            "💬 Ответить клиенту",
            callback_data=f"reply_tg_{tg_id}"
        )])

    staff_id = STORE_STAFF.get(shop_brand)
    if staff_id:
        safe_branch = shop_branch.replace("_", "-")
        buttons.append([InlineKeyboardButton(
            f"🏪 Передать в {BRAND_NAMES.get(shop_brand, shop_brand)}",
            callback_data=f"fwd_{staff_id}_{safe_branch[:40]}"
        )])

    kb = InlineKeyboardMarkup(buttons) if buttons else None

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id, text=msg,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"notify admin {admin_id}: {e}")


# ── Действия администраторов ────────────────────────────

async def cb_admin_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Нет прав.", show_alert=True)
        return

    data = query.data

    if data.startswith("reply_tg_"):
        client_id = int(data.replace("reply_tg_", ""))
        reply_mode[query.from_user.id] = {"user_id": client_id, "source": "tg"}
        await ctx.bot.send_message(
            chat_id=query.from_user.id,
            text=(
                "✍️ <b>Режим ответа</b>\n\n"
                "Напишите ответ — он придёт клиенту в бот.\n"
                "/cancel — отменить"
            ),
            parse_mode=ParseMode.HTML
        )

    elif data.startswith("fwd_"):
        parts = data.split("_", 2)
        try:
            staff_id   = int(parts[1])
            shop_label = parts[2] if len(parts) > 2 else "магазин"
        except Exception:
            return
        orig = query.message.text or ""
        try:
            await ctx.bot.send_message(
                chat_id=staff_id,
                text=(
                    f"📋 <b>Обращение по магазину {shop_label}:</b>\n\n"
                    f"{orig}"
                ),
                parse_mode=ParseMode.HTML
            )
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Передано", callback_data="noop_done")
                ]])
            )
        except Exception as e:
            await ctx.bot.send_message(
                chat_id=query.from_user.id,
                text=f"⚠️ Не удалось переслать сотруднику: {e}"
            )

    elif data in ("noop", "noop_done"):
        pass


async def _admin_send_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    admin_id = update.effective_user.id
    state    = reply_mode.get(admin_id)
    if not state:
        return ConversationHandler.END
    text = update.message.text.strip()
    if text.lower() in ("/cancel", "отмена"):
        del reply_mode[admin_id]
        await update.message.reply_text("Режим ответа отменён.")
        return ConversationHandler.END
    client_id = state["user_id"]
    try:
        await ctx.bot.send_message(
            chat_id=client_id,
            text=f"📩 <b>Ответ от ПродТовары:</b>\n\n{text}",
            parse_mode=ParseMode.HTML
        )
        await update.message.reply_text("✅ Ответ отправлен.")
        for other in ADMIN_IDS:
            if other != admin_id:
                try:
                    await ctx.bot.send_message(
                        chat_id=other,
                        text=f"ℹ️ Коллега ответил клиенту:\n\n{text}"
                    )
                except Exception:
                    pass
    except Exception as e:
        await update.message.reply_text(f"⚠️ Не удалось отправить: {e}")
    del reply_mode[admin_id]
    return ConversationHandler.END


async def admin_message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if is_admin(uid) and uid in reply_mode:
        await _admin_send_reply(update, ctx)


# ── Команды администратора ──────────────────────────────

async def cmd_staff(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    lines = [
        f"• {BRAND_NAMES.get(b, b)}: ID {sid}"
        for b, sid in STORE_STAFF.items()
    ]
    text = "\n".join(lines) if lines else (
        "Маршрутизация не настроена.\n"
        "Добавьте ID сотрудников в STORE_STAFF в коде бота.\n"
        "Узнать свой ID: @userinfobot"
    )
    await update.message.reply_text(
        f"🏪 <b>Ответственные по магазинам:</b>\n\n{text}",
        parse_mode=ParseMode.HTML
    )


# ── HTTP-сервер для web-отзывов ─────────────────────────

class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.info("HTTP: " + fmt % args)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

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
            length = int(self.headers.get("Content-Length", 0))
            data   = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            logger.error(f"JSON: {e}")
            self.send_response(400)
            self.end_headers()
            return

        shop = {
            "id":     data.get("shop_id", ""),
            "name":   data.get("shop_name", "Магазин"),
            "branch": data.get("branch", ""),
            "brand":  data.get("brand", ""),
        }
        text     = str(data.get("message", data.get("review", "")))
        name     = data.get("name", "")
        email    = data.get("email", "")
        phone    = data.get("phone", "")
        username = name or email or "Посетитель сайта"
        if phone:
            username += f" · {phone}"

        if _bot_loop and _app:
            asyncio.run_coroutine_threadsafe(
                _notify_admins(_app.bot, shop, text, 0, username, source="web"),
                _bot_loop
            )

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')


def _run_http():
    srv = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    logger.info(f"HTTP на порту {PORT}")
    srv.serve_forever()


# ── Main ────────────────────────────────────────────────

def main() -> None:
    global _app, _bot_loop

    app = Application.builder().token(BOT_TOKEN).build()
    _app = app

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            SELECTING_SHOP:  [CallbackQueryHandler(cb_select_shop, pattern=r"^(shop_|noop)")],
            WRITING_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_message)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_admin_action, pattern=r"^(reply_|fwd_|noop)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_message_handler))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("staff",  cmd_staff))

    threading.Thread(target=_run_http, daemon=True).start()

    async def _save_loop(a):
        global _bot_loop
        _bot_loop = asyncio.get_event_loop()

    app.post_init = _save_loop

    logger.info(f"Бот запущен. Администраторы: {ADMIN_IDS}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
