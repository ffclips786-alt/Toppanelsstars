#!/usr/bin/env python3
"""
⭐ Stars Shop Bot
Telegram Stars-powered digital product shop with phone verification.
"""

import os
import sqlite3
import logging
from datetime import datetime
from functools import wraps

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    LabeledPrice,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
_raw_owner = os.getenv("OWNER_ID", "0").strip()
try:
    OWNER_ID = int(_raw_owner) if _raw_owner else 0
except ValueError:
    OWNER_ID = 0

DB_PATH   = os.getenv("DB_PATH", "stars_shop.db")
SUPPORT   = "https://t.me/wgstrikes"
LINE      = "─────────────────"

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

if OWNER_ID == 0:
    logger.warning("OWNER_ID is not set — owner panel will not work.")


# ═══════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            phone       TEXT,
            verified    INTEGER DEFAULT 0,
            banned      INTEGER DEFAULT 0,
            joined_date TEXT
        );
        CREATE TABLE IF NOT EXISTS products (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            description TEXT,
            active      INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS variants (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id  INTEGER NOT NULL,
            name        TEXT NOT NULL,
            price_stars INTEGER NOT NULL,
            active      INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS stock (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            variant_id  INTEGER NOT NULL,
            key_value   TEXT NOT NULL,
            is_sold     INTEGER DEFAULT 0,
            sold_to     INTEGER,
            sold_date   TEXT
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            product_id  INTEGER,
            variant_id  INTEGER,
            key_value   TEXT,
            stars_paid  INTEGER,
            date        TEXT
        );
    """)
    conn.commit()
    conn.close()


# ── User helpers ─────────────────────────

def ensure_user(user_id, username, first_name):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if not c.fetchone():
        verified = 1 if user_id == OWNER_ID else 0
        c.execute(
            "INSERT INTO users (user_id,username,first_name,verified,banned,joined_date)"
            " VALUES (?,?,?,?,0,?)",
            (user_id, username, first_name, verified, datetime.utcnow().isoformat()),
        )
    else:
        c.execute(
            "UPDATE users SET username=?,first_name=? WHERE user_id=?",
            (username, first_name, user_id),
        )
        if user_id == OWNER_ID:
            c.execute("UPDATE users SET verified=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def get_user(uid):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    conn.close()
    return row


def set_phone(uid, phone):
    conn = get_conn()
    conn.execute("UPDATE users SET phone=?,verified=1 WHERE user_id=?", (phone, uid))
    conn.commit()
    conn.close()


def set_banned(uid, val: bool):
    conn = get_conn()
    conn.execute("UPDATE users SET banned=? WHERE user_id=?", (1 if val else 0, uid))
    conn.commit()
    conn.close()


def all_users():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id!=? ORDER BY joined_date DESC", (OWNER_ID,))
    rows = c.fetchall()
    conn.close()
    return rows


def find_user(identifier: str):
    identifier = identifier.strip().lstrip("@")
    conn = get_conn()
    c = conn.cursor()
    if identifier.isdigit():
        c.execute("SELECT * FROM users WHERE user_id=?", (int(identifier),))
    else:
        c.execute("SELECT * FROM users WHERE username=?", (identifier,))
    row = c.fetchone()
    conn.close()
    return row


def user_purchase_count(uid):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM transactions WHERE user_id=?", (uid,))
    n = c.fetchone()[0]
    conn.close()
    return n


# ── Product helpers ──────────────────────

def get_products(active_only=True):
    conn = get_conn()
    c = conn.cursor()
    q = "SELECT * FROM products WHERE active=1" if active_only else "SELECT * FROM products"
    c.execute(q + " ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return rows


def get_product(pid):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE id=?", (pid,))
    row = c.fetchone()
    conn.close()
    return row


def add_product(name, description):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO products (name,description) VALUES (?,?)", (name, description))
    pid = c.lastrowid
    conn.commit()
    conn.close()
    return pid


def toggle_product(pid, active: bool):
    conn = get_conn()
    conn.execute("UPDATE products SET active=? WHERE id=?", (1 if active else 0, pid))
    conn.commit()
    conn.close()


def get_variants(pid, active_only=True):
    conn = get_conn()
    c = conn.cursor()
    if active_only:
        c.execute("SELECT * FROM variants WHERE product_id=? AND active=1", (pid,))
    else:
        c.execute("SELECT * FROM variants WHERE product_id=?", (pid,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_variant(vid):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM variants WHERE id=?", (vid,))
    row = c.fetchone()
    conn.close()
    return row


def add_variant(pid, name, price_stars):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO variants (product_id,name,price_stars) VALUES (?,?,?)",
        (pid, name, price_stars),
    )
    vid = c.lastrowid
    conn.commit()
    conn.close()
    return vid


def toggle_variant(vid, active: bool):
    conn = get_conn()
    conn.execute("UPDATE variants SET active=? WHERE id=?", (1 if active else 0, vid))
    conn.commit()
    conn.close()


def add_stock_keys(vid, keys):
    conn = get_conn()
    conn.executemany(
        "INSERT INTO stock (variant_id,key_value) VALUES (?,?)",
        [(vid, k.strip()) for k in keys if k.strip()],
    )
    conn.commit()
    conn.close()


def stock_count(vid):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM stock WHERE variant_id=? AND is_sold=0", (vid,))
    n = c.fetchone()[0]
    conn.close()
    return n


def pop_key(vid, uid):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id,key_value FROM stock WHERE variant_id=? AND is_sold=0 LIMIT 1", (vid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    c.execute(
        "UPDATE stock SET is_sold=1,sold_to=?,sold_date=? WHERE id=?",
        (uid, datetime.utcnow().isoformat(), row["id"]),
    )
    conn.commit()
    conn.close()
    return row["key_value"]


def log_tx(uid, pid, vid, key, stars):
    conn = get_conn()
    conn.execute(
        "INSERT INTO transactions (user_id,product_id,variant_id,key_value,stars_paid,date)"
        " VALUES (?,?,?,?,?,?)",
        (uid, pid, vid, key, stars, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_history(uid, limit=15):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """
        SELECT t.*,p.name AS pname,v.name AS vname
        FROM transactions t
        LEFT JOIN products p ON t.product_id=p.id
        LEFT JOIN variants v ON t.variant_id=v.id
        WHERE t.user_id=?
        ORDER BY t.id DESC LIMIT ?
        """,
        (uid, limit),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def shop_stats():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users WHERE user_id!=?", (OWNER_ID,))
    users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE verified=1 AND user_id!=?", (OWNER_ID,))
    verified = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM transactions")
    sales = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(stars_paid),0) FROM transactions")
    stars = c.fetchone()[0]
    conn.close()
    return users, verified, sales, stars


# ═══════════════════════════════════════════
# UI HELPERS
# ═══════════════════════════════════════════

def clear(ctx):
    ctx.user_data.pop("awaiting", None)
    ctx.user_data.pop("temp", None)


async def send(update: Update, text: str, reply_markup=None):
    """Universal send: handles both message and callback contexts."""
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            await update.callback_query.message.reply_text(
                text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
            )
    else:
        await update.message.reply_text(
            text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
        )


# ═══════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════

def main_menu_kb(owner=False):
    rows = [
        [
            InlineKeyboardButton("🛍️  Shop", callback_data="shop"),
            InlineKeyboardButton("📦  My Orders", callback_data="orders"),
        ],
        [InlineKeyboardButton("📞  Support", url=SUPPORT)],
    ]
    if owner:
        rows.insert(1, [InlineKeyboardButton("⚙️  Owner Panel", callback_data="owner")])
    return InlineKeyboardMarkup(rows)


def owner_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦  Products", callback_data="o_products"),
            InlineKeyboardButton("👥  Users", callback_data="o_users"),
        ],
        [
            InlineKeyboardButton("📊  Stats", callback_data="o_stats"),
            InlineKeyboardButton("⬅️  Menu", callback_data="menu"),
        ],
    ])


def back(to="menu"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️  Back", callback_data=to)]])


def shop_kb(products):
    rows = [
        [InlineKeyboardButton(f"✨  {p['name']}", callback_data=f"p_{p['id']}")]
        for p in products
    ]
    rows.append([InlineKeyboardButton("⬅️  Back", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def product_kb(pid, variants):
    rows = []
    for v in variants:
        cnt = stock_count(v["id"])
        available = cnt > 0
        label = f"{'✅' if available else '❌'}  {v['name']}  —  ⭐ {v['price_stars']}"
        cb = f"buy_{v['id']}" if available else "oos"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])
    rows.append([InlineKeyboardButton("⬅️  Back to Shop", callback_data="shop")])
    return InlineKeyboardMarkup(rows)


def o_products_kb(products):
    rows = [
        [InlineKeyboardButton(
            f"{'🟢' if p['active'] else '🔴'}  {p['name']}",
            callback_data=f"op_{p['id']}"
        )]
        for p in products
    ]
    rows.append([InlineKeyboardButton("➕  Add Product", callback_data="o_addprod")])
    rows.append([InlineKeyboardButton("⬅️  Back", callback_data="owner")])
    return InlineKeyboardMarkup(rows)


def o_product_detail_kb(pid, variants):
    rows = []
    for v in variants:
        cnt = stock_count(v["id"])
        label = f"{v['name']}  ⭐{v['price_stars']}  ({cnt} keys)"
        rows.append([InlineKeyboardButton(label, callback_data=f"ov_{v['id']}")])
    rows += [
        [InlineKeyboardButton("➕  Add Variant", callback_data=f"o_addvar_{pid}")],
        [InlineKeyboardButton("🗑️  Delete Product", callback_data=f"o_delprod_{pid}")],
        [InlineKeyboardButton("⬅️  Back", callback_data="o_products")],
    ]
    return InlineKeyboardMarkup(rows)


def o_variant_kb(vid, pid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦  Add Stock Keys", callback_data=f"o_addstock_{vid}")],
        [InlineKeyboardButton("🗑️  Delete Variant", callback_data=f"o_delvar_{vid}_{pid}")],
        [InlineKeyboardButton("⬅️  Back", callback_data=f"op_{pid}")],
    ])


def o_users_kb(users):
    rows = []
    for u in users:
        name = u["username"] or u["first_name"] or str(u["user_id"])
        icon = "🚫" if u["banned"] else ("✅" if u["verified"] else "🔒")
        rows.append([InlineKeyboardButton(f"{icon}  {name}", callback_data=f"ou_{u['user_id']}")])
    rows.append([InlineKeyboardButton("⬅️  Back", callback_data="owner")])
    return InlineKeyboardMarkup(rows)


def o_user_action_kb(uid, banned):
    label = "✅  Unban" if banned else "🚫  Ban"
    action = f"o_unban_{uid}" if banned else f"o_ban_{uid}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=action)],
        [InlineKeyboardButton("⬅️  Back", callback_data="o_users")],
    ])


# ═══════════════════════════════════════════
# ACCESS CONTROL
# ═══════════════════════════════════════════

def is_owner(uid): return uid == OWNER_ID


def owner_only(fn):
    @wraps(fn)
    async def wrap(update, context):
        if not is_owner(update.effective_user.id):
            await send(update, "⛔ Owner only.")
            return
        return await fn(update, context)
    return wrap


# ═══════════════════════════════════════════
# SCREENS
# ═══════════════════════════════════════════

async def screen_menu(update, context):
    clear(context)
    u = update.effective_user
    owner = is_owner(u.id)
    text = (
        f"⭐ *Stars Shop*\n{LINE}\n"
        f"Hello, {u.first_name}!\n\n"
        "Buy digital products instantly\n"
        f"using Telegram Stars.\n{LINE}"
    )
    await send(update, text, reply_markup=main_menu_kb(owner))


async def screen_shop(update, context):
    products = get_products()
    if not products:
        await send(update, f"🛍️ *Shop*\n{LINE}\nNo products available yet.", reply_markup=back())
        return
    await send(update, f"🛍️ *Shop*\n{LINE}\nSelect a product:", reply_markup=shop_kb(products))


async def screen_product(update, context, pid):
    p = get_product(pid)
    if not p:
        await send(update, "❌ Product not found.", reply_markup=back("shop"))
        return
    variants = get_variants(pid)
    text = (
        f"✨ *{p['name']}*\n{LINE}\n"
        f"{p['description'] or 'No description.'}\n{LINE}\n"
        f"{'Select a plan:' if variants else '⚠️ No plans available yet.'}"
    )
    await send(update, text, reply_markup=product_kb(pid, variants))


async def screen_orders(update, context):
    uid = update.effective_user.id
    history = get_history(uid)
    if not history:
        await send(update, f"📦 *My Orders*\n{LINE}\nNo purchases yet.", reply_markup=back())
        return
    text = f"📦 *My Orders*\n{LINE}\n"
    for r in history:
        text += (
            f"✨ *{r['pname']}* — {r['vname']}\n"
            f"⭐ {r['stars_paid']} Stars   📅 {r['date'][:10]}\n"
            f"🔐 `{r['key_value']}`\n{LINE}\n"
        )
    await send(update, text, reply_markup=back())


async def screen_owner(update, context):
    clear(context)
    await send(update, f"⚙️ *Owner Panel*\n{LINE}\nManage your shop:", reply_markup=owner_kb())


async def screen_o_products(update, context):
    products = get_products(active_only=False)
    text = f"📦 *Products* ({len(products)} total)\n{LINE}"
    await send(update, text, reply_markup=o_products_kb(products))


async def screen_o_product(update, context, pid):
    p = get_product(pid)
    if not p:
        await send(update, "❌ Not found.", reply_markup=back("o_products"))
        return
    variants = get_variants(pid, active_only=False)
    status = "🟢 Active" if p["active"] else "🔴 Hidden"
    text = (
        f"📦 *{p['name']}*\n{LINE}\n"
        f"Status: {status}\n"
        f"Variants: {len(variants)}\n"
        f"{p['description'] or 'No description.'}\n{LINE}"
    )
    await send(update, text, reply_markup=o_product_detail_kb(pid, variants))


async def screen_o_users(update, context):
    users = all_users()
    if not users:
        await send(update, f"👥 *Users*\n{LINE}\nNo users yet.", reply_markup=back("owner"))
        return
    text = f"👥 *Users* ({len(users)} total)\n{LINE}\nTap a user for details:"
    await send(update, text, reply_markup=o_users_kb(users))


async def screen_o_user(update, context, target_uid):
    u = find_user(str(target_uid))
    if not u:
        await send(update, "❌ User not found.", reply_markup=back("o_users"))
        return
    purchases = user_purchase_count(target_uid)
    status = "🚫 Banned" if u["banned"] else ("✅ Verified" if u["verified"] else "🔒 Unverified")
    text = (
        f"👤 *User Info*\n{LINE}\n"
        f"🆔 ID: `{u['user_id']}`\n"
        f"📛 Name: {u['first_name'] or 'N/A'}\n"
        f"🔗 Username: {'@' + u['username'] if u['username'] else 'N/A'}\n"
        f"📱 Phone: `{u['phone'] or 'Not verified'}`\n"
        f"📅 Joined: {(u['joined_date'] or '')[:10] or 'N/A'}\n"
        f"🛍️ Total Purchases: {purchases}\n"
        f"Status: {status}\n{LINE}"
    )
    await send(update, text, reply_markup=o_user_action_kb(target_uid, bool(u["banned"])))


async def screen_o_stats(update, context):
    users, verified, sales, stars = shop_stats()
    text = (
        f"📊 *Shop Stats*\n{LINE}\n"
        f"👥 Total Users: {users}\n"
        f"✅ Verified: {verified}\n"
        f"🛍️ Total Sales: {sales}\n"
        f"⭐ Stars Earned: {stars}\n{LINE}"
    )
    await send(update, text, reply_markup=back("owner"))


# ═══════════════════════════════════════════
# COMMANDS
# ═══════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or "", u.first_name or "")
    user = get_user(u.id)

    if user and user["banned"]:
        await update.message.reply_text("🚫 You are banned. Contact support.")
        return

    if not is_owner(u.id) and (not user or not user["verified"]):
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📱  Share My Number", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text(
            f"👋 Welcome to *Stars Shop!*\n{LINE}\n\n"
            "To access the shop, please verify\nyour phone number first.\n\n"
            "Tap the button below 👇",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await screen_menu(update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear(context)
    await update.message.reply_text("✅ Cancelled.", reply_markup=ReplyKeyboardRemove())
    await screen_menu(update, context)


# ═══════════════════════════════════════════
# CONTACT HANDLER (phone verification)
# ═══════════════════════════════════════════

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    contact = update.message.contact
    ensure_user(u.id, u.username or "", u.first_name or "")
    set_phone(u.id, contact.phone_number)
    await update.message.reply_text(
        f"✅ *Verified!*\n{LINE}\n"
        f"Phone: `{contact.phone_number}`\n\n"
        "You now have full access to the shop!",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.MARKDOWN,
    )
    await screen_menu(update, context)


# ═══════════════════════════════════════════
# STARS PAYMENT
# ═══════════════════════════════════════════

async def do_send_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, vid: int):
    v = get_variant(vid)
    if not v:
        if update.callback_query:
            await update.callback_query.answer("❌ Not found.", show_alert=True)
        return
    p = get_product(v["product_id"])
    if stock_count(vid) == 0:
        if update.callback_query:
            await update.callback_query.answer("❌ Out of stock!", show_alert=True)
        return
    await context.bot.send_invoice(
        chat_id=update.effective_user.id,
        title=f"{p['name']} — {v['name']}",
        description=p["description"] or p["name"],
        payload=f"v_{vid}",
        provider_token="",       # empty string = Telegram Stars
        currency="XTR",
        prices=[LabeledPrice(v["name"], v["price_stars"])],
    )
    if update.callback_query:
        await update.callback_query.answer("⭐ Invoice sent!")


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    if not q.invoice_payload.startswith("v_"):
        await q.answer(ok=False, error_message="Invalid payment.")
        return
    vid = int(q.invoice_payload.split("_")[1])
    if stock_count(vid) == 0:
        await q.answer(ok=False, error_message="This item just went out of stock. Please try again later.")
        return
    await q.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    vid   = int(payment.invoice_payload.split("_")[1])
    stars = payment.total_amount
    uid   = update.effective_user.id
    now   = datetime.utcnow()

    v   = get_variant(vid)
    p   = get_product(v["product_id"])
    key = pop_key(vid, uid)

    if not key:
        await update.message.reply_text(
            "⚠️ Payment received but stock ran out.\n"
            "Please contact support immediately!",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    log_tx(uid, v["product_id"], vid, key, stars)

    await update.message.reply_text(
        f"🎉 *Purchase Complete!*\n{LINE}\n"
        f"✨ Product: {p['name']}\n"
        f"📋 Plan: {v['name']}\n"
        f"⭐ Stars Paid: {stars}\n"
        f"📅 Date: {now.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"{LINE}\n"
        f"🔐 *Your Key:*\n`{key}`\n"
        f"{LINE}\n"
        "Tap *My Orders* anytime to see your keys.",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Notify owner
    if OWNER_ID:
        buyer_name = f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.first_name
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    f"⭐ *New Sale!*\n{LINE}\n"
                    f"👤 Buyer: {buyer_name} (`{uid}`)\n"
                    f"✨ {p['name']} — {v['name']}\n"
                    f"⭐ Stars: {stars}\n"
                    f"🔐 Key: `{key}`\n"
                    f"📅 {now.strftime('%Y-%m-%d')}\n"
                    f"🕒 {now.strftime('%H:%M UTC')}\n{LINE}"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            logger.exception("Failed to notify owner of sale.")


# ═══════════════════════════════════════════
# TEXT INPUT HANDLER (owner multi-step flows)
# ═══════════════════════════════════════════

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        return
    if not is_owner(update.effective_user.id):
        clear(context)
        return

    text = update.message.text.strip()
    temp = context.user_data.setdefault("temp", {})

    # ── Add product ──────────────────────────
    if awaiting == "prod_name":
        temp["name"] = text
        context.user_data["awaiting"] = "prod_desc"
        await update.message.reply_text(
            '📝 Send the *product description*\n(or type "skip" to leave empty):',
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if awaiting == "prod_desc":
        desc = "" if text.lower() == "skip" else text
        pid  = add_product(temp["name"], desc)
        clear(context)
        await update.message.reply_text(
            f"✅ Product *{temp['name']}* created!\n"
            f"Now open it from the Products panel to add variants.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=owner_kb(),
        )
        return

    # ── Add variant ──────────────────────────
    if awaiting == "var_name":
        temp["vname"] = text
        context.user_data["awaiting"] = "var_price"
        await update.message.reply_text(
            "⭐ Send the *price in Stars* (whole number only):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if awaiting == "var_price":
        try:
            price = int(text)
            if price < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Invalid. Send a whole number (e.g. 50).")
            return
        vid = add_variant(temp["product_id"], temp["vname"], price)
        pid = temp["product_id"]
        clear(context)
        await update.message.reply_text(
            f"✅ Variant *{temp['vname']}* added at ⭐{price}!\n"
            "Now add stock keys from the variant panel.",
            parse_mode=ParseMode.MARKDOWN,
        )
        v   = get_variant(vid)
        cnt = stock_count(vid)
        await update.message.reply_text(
            f"📋 *{v['name']}* — ⭐{v['price_stars']} | {cnt} keys",
            reply_markup=o_variant_kb(vid, pid),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── Add stock keys ───────────────────────
    if awaiting == "stock_keys":
        raw  = text.replace(",", "\n")
        keys = [k.strip() for k in raw.split("\n") if k.strip()]
        vid  = temp["variant_id"]
        pid  = temp["product_id"]
        add_stock_keys(vid, keys)
        clear(context)
        v = get_variant(vid)
        await update.message.reply_text(
            f"✅ Added *{len(keys)}* key(s) to *{v['name']}*.\n"
            f"Total in stock: {stock_count(vid)}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=o_variant_kb(vid, pid),
        )
        return


# ═══════════════════════════════════════════
# BUTTON HANDLER
# ═══════════════════════════════════════════

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    d   = q.data
    uid = update.effective_user.id

    # ── Shared ──────────────────────────────
    if d == "menu":
        await screen_menu(update, context)
        return
    if d == "shop":
        u = get_user(uid)
        if u and u["banned"]:
            await q.answer("🚫 You are banned.", show_alert=True)
            return
        await screen_shop(update, context)
        return
    if d == "orders":
        await screen_orders(update, context)
        return
    if d == "oos":
        await q.answer("❌ Out of stock!", show_alert=True)
        return
    if d.startswith("p_"):
        await screen_product(update, context, int(d[2:]))
        return
    if d.startswith("buy_"):
        await do_send_invoice(update, context, int(d[4:]))
        return

    # ── Owner only ───────────────────────────
    if not is_owner(uid):
        await q.answer("Owner only.", show_alert=True)
        return

    if d == "owner":
        await screen_owner(update, context)
        return
    if d == "o_products":
        await screen_o_products(update, context)
        return
    if d == "o_users":
        await screen_o_users(update, context)
        return
    if d == "o_stats":
        await screen_o_stats(update, context)
        return

    # Product management
    if d.startswith("op_"):
        await screen_o_product(update, context, int(d[3:]))
        return
    if d == "o_addprod":
        context.user_data.update(awaiting="prod_name", temp={})
        await q.answer()
        await q.message.reply_text("📦 Send the *product name*:", parse_mode=ParseMode.MARKDOWN)
        return
    if d.startswith("o_addvar_"):
        pid = int(d.split("_")[2])
        context.user_data.update(awaiting="var_name", temp={"product_id": pid})
        await q.answer()
        await q.message.reply_text(
            "🏷️ Send the *variant name*\n(e.g. '1 Month', 'Basic', 'VIP'):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if d.startswith("ov_"):
        vid = int(d[3:])
        v   = get_variant(vid)
        cnt = stock_count(vid)
        await send(
            update,
            f"📋 *{v['name']}*\n{LINE}\n"
            f"⭐ Price: {v['price_stars']} Stars\n"
            f"📦 Keys in stock: {cnt}\n{LINE}",
            reply_markup=o_variant_kb(vid, v["product_id"]),
        )
        return
    if d.startswith("o_addstock_"):
        vid = int(d.split("_")[2])
        v   = get_variant(vid)
        context.user_data.update(awaiting="stock_keys", temp={"variant_id": vid, "product_id": v["product_id"]})
        await q.answer()
        await q.message.reply_text(
            "🔐 Send the *keys* for this variant.\nOne per line or comma-separated:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if d.startswith("o_delvar_"):
        parts = d.split("_")
        vid, pid = int(parts[2]), int(parts[3])
        toggle_variant(vid, False)
        await q.answer("🗑️ Variant deleted.")
        await screen_o_product(update, context, pid)
        return
    if d.startswith("o_delprod_"):
        pid = int(d.split("_")[2])
        toggle_product(pid, False)
        await q.answer("🗑️ Product deleted.")
        await screen_o_products(update, context)
        return

    # User management
    if d.startswith("ou_"):
        await screen_o_user(update, context, int(d[3:]))
        return
    if d.startswith("o_ban_"):
        target = int(d.split("_")[2])
        set_banned(target, True)
        await q.answer("🚫 User banned.")
        await screen_o_user(update, context, target)
        return
    if d.startswith("o_unban_"):
        target = int(d.split("_")[2])
        set_banned(target, False)
        await q.answer("✅ User unbanned.")
        await screen_o_user(update, context, target)
        return


# ═══════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set.")
    logger.info(f"Stars Shop Bot starting | OWNER_ID={OWNER_ID}")
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("Polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
