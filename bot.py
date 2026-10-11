
import os
import asyncio
import sqlite3
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
)

TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PAY_TO = "@Haiskav2"
DB = "shop.db"

bot = Bot(TOKEN)
dp = Dispatcher()

conn = sqlite3.connect(DB)
conn.execute("""CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0,
    referrer INTEGER DEFAULT 0, rewarded INTEGER DEFAULT 0)""")
conn.execute("""CREATE TABLE IF NOT EXISTS requests(
    id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER,
    stars INTEGER, status TEXT DEFAULT 'pending')""")
conn.execute("""CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER,
    service TEXT, amount INTEGER, link TEXT, cost INTEGER,
    status TEXT DEFAULT 'new')""")
conn.commit()

# Цены указаны за 1000 единиц услуги, в монетах.
SERVICES = {
    "tg_sub": ("ТГ — подписчики", 5000),
    "tg_view": ("ТГ — просмотры", 1000),
    "tt_sub": ("ТТ — подписчики", 6000),
    "tt_like": ("ТТ — реакции", 2000),
}

def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
        for row in rows
    ])

def main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="💰 Баланс"),
         KeyboardButton(text="➕ Пополнение")],
        [KeyboardButton(text="📢 Накрутка ТГ"),
         KeyboardButton(text="🎵 Накрутка ТТ")],
        [KeyboardButton(text="👥 Пригласить друга"),
         KeyboardButton(text="📦 Мои заказы")]
    ], resize_keyboard=True)

def ensure_user(uid, ref=0):
    row = conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        if ref == uid:
            ref = 0
        conn.execute("INSERT INTO users(id,referrer) VALUES(?,?)",
                     (uid, ref))
        conn.commit()

async def home(message, text="Выбери действие в меню:"):
    await message.answer(text, reply_markup=main_menu())

@dp.message(CommandStart())
async def start(message: Message):
    args = (message.text or "").split(maxsplit=1)
    ref = 0
    if len(args) > 1 and args[1].isdigit():
        ref = int(args[1])
    ensure_user(message.from_user.id, ref)
    await home(message,
        "Добро пожаловать в магазин!\n\n"
        "Выбирай услугу, пополняй баланс и оформляй заказ.\n"
        "За первую подтверждённую покупку приглашённого "
        "оба получите по 1000 монет.")

@dp.message(Command("id"))
async def show_id(message: Message):
    await message.answer(f"Твой Telegram ID: {message.from_user.id}")

@dp.message(F.text == "💰 Баланс")
async def balance(message: Message):
    ensure_user(message.from_user.id)
    amount = conn.execute(
        "SELECT balance FROM users WHERE id=?",
        (message.from_user.id,)
    ).fetchone()[0]
    await home(message, f"💰 Твой баланс: {amount:,} монет")

@dp.message(F.text == "➕ Пополнение")
async def topup(message: Message):
    await message.answer(
        f"Пополнение баланса\n\n"
        f"1 звезда = 1000 монет.\n"
        f"Отправь подарок аккаунту {PAY_TO}.\n"
        "После отправки нажми кнопку и укажи количество звёзд.\n"
        "Администратор проверит поступление вручную.",
        reply_markup=kb([[("⭐ Создать заявку", "topup")],
                         [("⬅️ Меню", "home")]])
    )

@dp.callback_query(F.data == "topup")
async def topup_form(call: CallbackQuery):
    await call.message.answer(
        "Напиши количество звёзд числом, например: 10"
    )
    await call.answer()
    # Следующее числовое сообщение обработает handler ниже.
    # Сохраняем ожидание заявки в памяти.
    waiting_topup.add(call.from_user.id)

waiting_topup = set()
waiting_order = {}

@dp.message(F.text)
async def text_handler(message: Message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    if uid in waiting_topup:
        waiting_topup.discard(uid)
        if not text.isdigit() or not (1 <= int(text) <= 100000):
            await message.answer("Введи целое число звёзд от 1 до 100000.")
            return
        stars = int(text)
        cur = conn.execute(
            "INSERT INTO requests(uid,stars) VALUES(?,?)", (uid, stars)
        )
        conn.commit()
        rid = cur.lastrowid
        await message.answer(
            f"Заявка #{rid} создана на {stars} ⭐.\n"
            "Баланс пополнится только после проверки администратором.",
            reply_markup=main_menu()
        )
        if ADMIN_ID:
            await bot.send_message(
                ADMIN_ID,
                f"💳 Заявка #{rid}\nПользователь: {uid}\n"
                f"Сумма: {stars} ⭐ ({stars*1000} монет)",
                reply_markup=kb([
                    [("✅ Одобрить", f"approve:{rid}"),
                     ("❌ Отклонить", f"reject:{rid}")]
                ])
            )
        return

    if uid in waiting_order:
        state = waiting_order.pop(uid)
        if state["step"] == "amount":
            if not text.isdigit() or int(text) < 100:
                waiting_order[uid] = state
                await message.answer("Укажи количество, минимум 100.")
                return
            state["amount"] = int(text)
            state["step"] = "link"
            waiting_order[uid] = state
            await message.answer("Теперь отправь ссылку на публикацию или профиль.")
            return
        if state["step"] == "link":
            service = state["service"]
            amount = state["amount"]
            cost = (amount * SERVICES[service][1] + 999) // 1000
            bal = conn.execute(
                "SELECT balance FROM users WHERE id=?", (uid,)
            ).fetchone()[0]
            if bal < cost:
                await message.answer(
                    f"Недостаточно монет. Нужно {cost}, у тебя {bal}.",
                    reply_markup=main_menu()
                )
                return
            cur = conn.execute(
                "INSERT INTO orders(uid,service,amount,link,cost) "
                "VALUES(?,?,?,?,?)",
                (uid, service, amount, text, cost)
            )
            oid = cur.lastrowid
            conn.execute("UPDATE users SET balance=balance-? WHERE id=?",
                         (cost, uid))
            conn.commit()
            await message.answer(
                f"✅ Заказ #{oid} принят!\nСписано: {cost} монет.\n"
                "Выполнение — вручную администратором.",
                reply_markup=main_menu()
            )
            if ADMIN_ID:
                await bot.send_message(
                    ADMIN_ID,
                    f"📦 Заказ #{oid}\nПользователь: {uid}\n"
                    f"Услуга: {SERVICES[service][0]}\n"
                    f"Количество: {amount}\nСсылка: {text}\n"
                    f"Стоимость: {cost} монет",
                    reply_markup=kb([
                        [("✅ Выполнен", f"done:{oid}"),
                         ("↩️ Возврат", f"refund:{oid}")]
                    ])
                )
            return

    if text == "📢 Накрутка ТГ":
        await message.answer("Выбери услугу:",
            reply_markup=kb([
                [("Подписчики", "svc:tg_sub"),
                 ("Просмотры", "svc:tg_view")]
            ]))
    elif text == "🎵 Накрутка ТТ":
        await message.answer("Выбери услугу:",
            reply_markup=kb([
                [("Подписчики", "svc:tt_sub"),
                 ("Реакции", "svc:tt_like")]
            ]))
    elif text == "👥 Пригласить друга":
        me = await bot.get_me()
        await message.answer(
            f"Твоя ссылка:\nhttps://t.me/{me.username}?start={uid}\n\n"
            "После первой подтверждённой покупки друга "
            "вы оба получите по 1000 монет."
        )
    elif text == "📦 Мои заказы":
        rows = conn.execute(
            "SELECT id,service,amount,cost,status FROM orders "
            "WHERE uid=? ORDER BY id DESC LIMIT 10", (uid,)
        ).fetchall()
        if not rows:
            await message.answer("У тебя пока нет заказов.")
        else:
            await message.answer("\n".join(
                f"#{r[0]} — {SERVICES[r[1]][0]}, {r[2]} шт., "
                f"{r[3]} монет — {r[4]}" for r in rows
            ))
    else:
        await message.answer("Используй кнопки меню.", reply_markup=main_menu())

@dp.callback_query(F.data == "home")
async def go_home(call: CallbackQuery):
    await call.message.answer("Главное меню:", reply_markup=main_menu())
    await call.answer()

@dp.callback_query(F.data.startswith("svc:"))
async def choose_service(call: CallbackQuery):
    service = call.data.split(":")[1]
    if service not in SERVICES:
        await call.answer("Услуга не найдена", show_alert=True)
        return
    waiting_order[call.from_user.id] = {
        "service": service, "step": "amount"
    }
    await call.message.answer(
        f"{SERVICES[service][0]}\n"
        f"Цена за 1000: {SERVICES[service][1]} монет.\n"
        "Введи количество (минимум 100)."
    )
    await call.answer()

@dp.callback_query(F.data.startswith(("approve:", "reject:")))
async def topup_admin(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return
    action, raw = call.data.split(":")
    rid = int(raw)
    row = conn.execute(
        "SELECT uid,stars,status FROM requests WHERE id=?", (rid,)
    ).fetchone()
    if not row or row[2] != "pending":
        await call.answer("Заявка уже обработана", show_alert=True)
        return
    uid, stars, _ = row
    if action == "approve":
        conn.execute("UPDATE users SET balance=balance+? WHERE id=?",
                     (stars*1000, uid))
        conn.execute("UPDATE requests SET status='approved' WHERE id=?",
                     (rid,))
        conn.commit()
        await bot.send_message(uid,
            f"✅ Пополнение одобрено: +{stars*1000} монет.")
    else:
        conn.execute("UPDATE requests SET status='rejected' WHERE id=?",
                     (rid,))
        conn.commit()
        await bot.send_message(uid, "❌ Заявка на пополнение отклонена.")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Готово")

@dp.callback_query(F.data.startswith(("done:", "refund:")))
async def order_admin(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return
    action, raw = call.data.split(":")
    oid = int(raw)
    row = conn.execute(
        "SELECT uid,cost,status FROM orders WHERE id=?", (oid,)
    ).fetchone()
    if not row or row[2] != "new":
        await call.answer("Заказ уже обработан", show_alert=True)
        return
    uid, cost, _ = row
    if action == "refund":
        conn.execute("UPDATE users SET balance=balance+? WHERE id=?",
                     (cost, uid))
        status = "refunded"
        await bot.send_message(uid, f"↩️ Заказ #{oid} отменён, {cost} монет возвращены.")
    else:
        status = "done"
        await bot.send_message(uid, f"✅ Заказ #{oid} выполнен.")
    conn.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
    conn.commit()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Готово")

async def health(request):
    return web.Response(text="Bot is running")

async def main():
    if not TOKEN:
        raise RuntimeError("Set BOT_TOKEN in hosting environment variables")
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
  
