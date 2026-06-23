#!/usr/bin/env python3
"""
⭐ Stars Shop Bot
Telegram Stars-powered digital product shop with phone verification.

v2 — added: multi-language customer UI, owner broadcast/announcements,
bot statistics, maintenance mode, smoother + crash-proof flows.
"""

import os
import asyncio
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
from telegram.helpers import escape_markdown
from telegram.error import BadRequest, Forbidden
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
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()

    # ── migrations for older databases ──
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "language" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'en'")

    conn.execute("PRAGMA journal_mode=WAL")
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
            "INSERT INTO users (user_id,username,first_name,verified,banned,joined_date,language)"
            " VALUES (?,?,?,?,0,?,'en')",
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


def get_lang(uid):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT language FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    conn.close()
    lang = row["language"] if row and row["language"] else "en"
    return lang if lang in TEXTS else "en"


def set_lang(uid, lang):
    conn = get_conn()
    conn.execute("UPDATE users SET language=? WHERE user_id=?", (lang, uid))
    conn.commit()
    conn.close()


# ── Settings helpers (maintenance mode etc.) ─────────────

def get_setting(key, default=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def is_maintenance():
    return get_setting("maintenance", "0") == "1"


def set_maintenance(val: bool):
    set_setting("maintenance", "1" if val else "0")


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
    c.execute("SELECT COUNT(*), COALESCE(SUM(stars_paid),0) FROM transactions")
    total_sales, total_revenue = c.fetchone()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    c.execute(
        "SELECT COUNT(*), COALESCE(SUM(stars_paid),0) FROM transactions WHERE date LIKE ?",
        (today + "%",),
    )
    today_sales, today_revenue = c.fetchone()
    conn.close()
    return {
        "users": users,
        "total_sales": total_sales,
        "total_revenue": total_revenue,
        "today_sales": today_sales,
        "today_revenue": today_revenue,
    }


# ═══════════════════════════════════════════
# LANGUAGES / TRANSLATIONS  (customer-facing UI)
# ═══════════════════════════════════════════

LANGS = {
    "en": "🇬🇧 English",
    "ur": "🇵🇰 اردو",
    "hi": "🇮🇳 हिंदी",
}

TEXTS = {
    "en": {
        "verify_prompt": "👋 Welcome to *Stars Shop!*\n{line}\n\nTo access the shop, please verify\nyour phone number first.\n\nTap the button below 👇",
        "share_number": "📱  Share My Number",
        "verify_done": "✅ *Verified!*\n{line}\nPhone: `{phone}`\n\nYou now have full access to the shop!",
        "menu_text": "⭐ *Stars Shop*\n{line}\nHello, {name}!\n\nBuy digital products instantly\nusing Telegram Stars.\n{line}",
        "btn_shop": "🛍️  Shop",
        "btn_mykeys": "🔑  My Keys",
        "btn_language": "🌐  Language",
        "btn_support": "📞  Support",
        "btn_owner": "⚙️  Owner Panel",
        "btn_back": "⬅️  Back",
        "btn_back_shop": "⬅️  Back to Shop",
        "shop_title": "🛍️ *Shop*\n{line}\nSelect a product:",
        "shop_empty": "🛍️ *Shop*\n{line}\nNo products available yet.",
        "product_text": "✨ *{name}*\n{line}\n{desc}\n{line}\n{hint}",
        "no_desc": "No description.",
        "select_plan": "Select a plan:",
        "no_plans": "⚠️ No plans available yet.",
        "keys_title": "🔑 *My Keys*\n{line}\n",
        "keys_empty": "🔑 *My Keys*\n{line}\nYou haven't purchased anything yet.",
        "keys_entry": "✨ *{pname}* — {vname}\n⭐ {stars} Stars   📅 {date}\n🔐 `{key}`\n{line}\n",
        "maintenance": "🛠️ *Bot is under maintenance.*\nWait until it gets fixed.",
        "banned": "🚫 You are banned. Contact support.",
        "lang_title": "🌐 *Choose your language:*",
        "lang_set": "✅ Language set to English!",
        "invoice_sent": "⭐ Invoice sent!",
        "out_of_stock": "❌ Out of stock!",
        "purchase_complete": "🎉 *Purchase Complete!*\n{line}\n✨ Product: {pname}\n📋 Plan: {vname}\n⭐ Stars Paid: {stars}\n📅 Date: {date}\n{line}\n🔐 *Your Key:*\n`{key}`\n{line}\nTap *My Keys* anytime to see your keys.",
        "stockout": "⚠️ Payment received but stock ran out.\nPlease contact support immediately!",
    },
    "ur": {
        "verify_prompt": "👋 سٹارز شاپ میں خوش آمدید!\n{line}\n\nشاپ تک رسائی کے لیے پہلے اپنا فون نمبر تصدیق کریں۔\n\nنیچے دیا گیا بٹن دبائیں 👇",
        "share_number": "📱  میرا نمبر شیئر کریں",
        "verify_done": "✅ تصدیق مکمل!\n{line}\nفون: `{phone}`\n\nاب آپ کو شاپ تک مکمل رسائی حاصل ہے!",
        "menu_text": "⭐ سٹارز شاپ\n{line}\nخوش آمدید، {name}!\n\nٹیلیگرام سٹارز سے فوری ڈیجیٹل پراڈکٹس خریدیں۔\n{line}",
        "btn_shop": "🛍️  شاپ",
        "btn_mykeys": "🔑  میری کیز",
        "btn_language": "🌐  زبان",
        "btn_support": "📞  سپورٹ",
        "btn_owner": "⚙️  اونر پینل",
        "btn_back": "⬅️  واپس",
        "btn_back_shop": "⬅️  شاپ پر واپس",
        "shop_title": "🛍️ شاپ\n{line}\nپراڈکٹ منتخب کریں:",
        "shop_empty": "🛍️ شاپ\n{line}\nابھی کوئی پراڈکٹ موجود نہیں۔",
        "product_text": "✨ {name}\n{line}\n{desc}\n{line}\n{hint}",
        "no_desc": "کوئی تفصیل موجود نہیں۔",
        "select_plan": "پلان منتخب کریں:",
        "no_plans": "⚠️ ابھی کوئی پلان دستیاب نہیں۔",
        "keys_title": "🔑 میری کیز\n{line}\n",
        "keys_empty": "🔑 میری کیز\n{line}\nآپ نے ابھی کچھ نہیں خریدا۔",
        "keys_entry": "✨ {pname} — {vname}\n⭐ {stars} سٹارز   📅 {date}\n🔐 `{key}`\n{line}\n",
        "maintenance": "🛠️ بوٹ میں مینٹیننس جاری ہے۔\nٹھیک ہونے تک انتظار کریں۔",
        "banned": "🚫 آپ کو بین کر دیا گیا ہے۔ سپورٹ سے رابطہ کریں۔",
        "lang_title": "🌐 اپنی زبان منتخب کریں:",
        "lang_set": "✅ زبان اردو میں سیٹ ہو گئی!",
        "invoice_sent": "⭐ انوائس بھیج دی گئی!",
        "out_of_stock": "❌ سٹاک ختم ہے!",
        "purchase_complete": "🎉 خریداری مکمل!\n{line}\n✨ پراڈکٹ: {pname}\n📋 پلان: {vname}\n⭐ ادا شدہ سٹارز: {stars}\n📅 تاریخ: {date}\n{line}\n🔐 آپ کی کی:\n`{key}`\n{line}\nاپنی کیز دیکھنے کے لیے کبھی بھی 'میری کیز' دبائیں۔",
        "stockout": "⚠️ ادائیگی موصول ہو گئی لیکن سٹاک ختم ہو گیا۔\nفوری سپورٹ سے رابطہ کریں!",
    },
    "hi": {
        "verify_prompt": "👋 स्टार्स शॉप में आपका स्वागत है!\n{line}\n\nशॉप एक्सेस करने के लिए पहले अपना फ़ोन नंबर वेरिफ़ाई करें।\n\nनीचे दिया बटन दबाएँ 👇",
        "share_number": "📱  मेरा नंबर शेयर करें",
        "verify_done": "✅ वेरिफ़ाई हो गया!\n{line}\nफ़ोन: `{phone}`\n\nअब आपको शॉप का पूरा एक्सेस मिल गया है!",
        "menu_text": "⭐ स्टार्स शॉप\n{line}\nनमस्ते, {name}!\n\nटेलीग्राम स्टार्स से तुरंत डिजिटल प्रोडक्ट्स खरीदें।\n{line}",
        "btn_shop": "🛍️  शॉप",
        "btn_mykeys": "🔑  मेरी कीज़",
        "btn_language": "🌐  भाषा",
        "btn_support": "📞  सपोर्ट",
        "btn_owner": "⚙️  ओनर पैनल",
        "btn_back": "⬅️  वापस",
        "btn_back_shop": "⬅️  शॉप पर वापस",
        "shop_title": "🛍️ शॉप\n{line}\nप्रोडक्ट चुनें:",
        "shop_empty": "🛍️ शॉप\n{line}\nअभी कोई प्रोडक्ट उपलब्ध नहीं है।",
        "product_text": "✨ {name}\n{line}\n{desc}\n{line}\n{hint}",
        "no_desc": "कोई विवरण नहीं।",
        "select_plan": "प्लान चुनें:",
        "no_plans": "⚠️ अभी कोई प्लान उपलब्ध नहीं है।",
        "keys_title": "🔑 मेरी कीज़\n{line}\n",
        "keys_empty": "🔑 मेरी कीज़\n{line}\nआपने अभी तक कुछ नहीं खरीदा।",
        "keys_entry": "✨ {pname} — {vname}\n⭐ {stars} स्टार्स   📅 {date}\n🔐 `{key}`\n{line}\n",
        "maintenance": "🛠️ बॉट अभी मेंटेनेंस में है।\nठीक होने तक इंतज़ार करें।",
        "banned": "🚫 आपको बैन कर दिया गया है। सपोर्ट से संपर्क करें।",
        "lang_title": "🌐 अपनी भाषा चुनें:",
        "lang_set": "✅ भाषा हिंदी में सेट हो गई!",
        "invoice_sent": "⭐ इनवॉइस भेज दिया गया!",
        "out_of_stock": "❌ स्टॉक ख़त्म है!",
        "purchase_complete": "🎉 खरीदारी पूरी हुई!\n{line}\n✨ प्रोडक्ट: {pname}\n📋 प्लान: {vname}\n⭐ स्टार्स भुगतान: {stars}\n📅 तारीख: {date}\n{line}\n🔐 आपकी की:\n`{key}`\n{line}\nअपनी कीज़ देखने के लिए कभी भी 'मेरी कीज़' दबाएँ।",
        "stockout": "⚠️ पेमेंट मिल गया लेकिन स्टॉक ख़त्म हो गया।\nफ़ौरन सपोर्ट से संपर्क करें!",
    },
}


def t(lang, key, **kwargs):
    lang = lang if lang in TEXTS else "en"
    template = TEXTS.get(lang, {}).get(key) or TEXTS["en"].get(key, key)
    try:
        return template.format(**kwargs)
    except Exception:
        return template


# ═══════════════════════════════════════════
# UI HELPERS
# ═══════════════════════════════════════════

def clear(ctx):
    ctx.user_data.pop("awaiting", None)
    ctx.user_data.pop("temp", None)


def md(text):
    """Escape user-supplied text so it can never break Markdown parsing."""
    return escape_markdown(str(text) if text is not None else "", version=1)


async def safe_answer(q, *args, **kwargs):
    """Answer a callback query without ever crashing the update
    (Telegram rejects answering the same query twice / after timeout)."""
    try:
        await q.answer(*args, **kwargs)
    except Exception:
        pass


async def send(update: Update, text: str, reply_markup=None):
    """Universal send: handles both message and callback contexts.
    Fully defensive — a failure here will never silently kill the bot."""
    if update.callback_query:
        await safe_answer(update.callback_query)
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            try:
                await update.callback_query.message.reply_text(
                    text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                logger.exception("send(): failed to deliver message to user.")
    else:
        try:
            await update.message.reply_text(
                text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            logger.exception("send(): failed to deliver message to user.")


# ═══════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════

def main_menu_kb(lang, owner=False):
    rows = [
        [
            InlineKeyboardButton(t(lang, "btn_shop"), callback_data="shop"),
            InlineKeyboardButton(t(lang, "btn_mykeys"), callback_data="orders"),
        ],
        [InlineKeyboardButton(t(lang, "btn_language"), callback_data="lang_menu")],
        [InlineKeyboardButton(t(lang, "btn_support"), url=SUPPORT)],
    ]
    if owner:
        rows.insert(2, [InlineKeyboardButton(t(lang, "btn_owner"), callback_data="owner")])
    return InlineKeyboardMarkup(rows)


def language_kb():
    rows = [[InlineKeyboardButton(label, callback_data=f"setlang_{code}")] for code, label in LANGS.items()]
    rows.append([InlineKeyboardButton("⬅️  Back", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def owner_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦  Products", callback_data="o_products"),
            InlineKeyboardButton("👥  Users", callback_data="o_users"),
        ],
        [InlineKeyboardButton("📊  Bot Statistics", callback_data="o_stats")],
        [
            InlineKeyboardButton("📣  Broadcast", callback_data="o_broadcast"),
            InlineKeyboardButton("🔧  Bot Settings", callback_data="o_settings"),
        ],
        [InlineKeyboardButton("⬅️  Menu", callback_data="menu")],
    ])


def back(to="menu", lang="en"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "btn_back"), callback_data=to)]])


def shop_kb(products, lang="en"):
    rows = [
        [InlineKeyboardButton(f"✨  {p['name']}", callback_data=f"p_{p['id']}")]
        for p in products
    ]
    rows.append([InlineKeyboardButton(t(lang, "btn_back"), callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def product_kb(pid, variants, lang="en"):
    rows = []
    for v in variants:
        cnt = stock_count(v["id"])
        available = cnt > 0
        label = f"{'✅' if available else '❌'}  {v['name']}  —  ⭐ {v['price_stars']}"
        cb = f"buy_{v['id']}" if available else "oos"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])
    rows.append([InlineKeyboardButton(t(lang, "btn_back_shop"), callback_data="shop")])
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
        [InlineKeyboardButton("➕  Add Plan", callback_data=f"o_addvar_{pid}")],
        [InlineKeyboardButton("🗑️  Delete Product", callback_data=f"o_delprod_{pid}")],
        [InlineKeyboardButton("⬅️  Back", callback_data="o_products")],
    ]
    return InlineKeyboardMarkup(rows)


def o_variant_kb(vid, pid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦  Add Stock Keys", callback_data=f"o_addstock_{vid}")],
        [InlineKeyboardButton("🗑️  Delete Plan", callback_data=f"o_delvar_{vid}_{pid}")],
        [InlineKeyboardButton("⬅️  Back", callback_data=f"op_{pid}")],
    ])


def o_next_step_kb(pid):
    """Shown right after stock keys are added — lets owner add another plan
    or finish, without extra menu hunting."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕  Add Another Plan", callback_data=f"o_addvar_{pid}")],
        [InlineKeyboardButton("✅  Done", callback_data=f"op_{pid}")],
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


def o_settings_kb(maint: bool):
    label = "✅  Disable Maintenance" if maint else "🛠️  Enable Maintenance"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="o_togglemaint")],
        [InlineKeyboardButton("⬅️  Back", callback_data="owner")],
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
# SCREENS — CUSTOMER SIDE (translated)
# ═══════════════════════════════════════════

async def screen_menu(update, context):
    clear(context)
    u = update.effective_user
    owner = is_owner(u.id)
    lang = get_lang(u.id)
    text = t(lang, "menu_text", line=LINE, name=md(u.first_name or ""))
    await send(update, text, reply_markup=main_menu_kb(lang, owner))


async def screen_shop(update, context):
    lang = get_lang(update.effective_user.id)
    products = get_products()
    if not products:
        await send(update, t(lang, "shop_empty", line=LINE), reply_markup=back(lang=lang))
        return
    await send(update, t(lang, "shop_title", line=LINE), reply_markup=shop_kb(products, lang))


async def screen_product(update, context, pid):
    lang = get_lang(update.effective_user.id)
    p = get_product(pid)
    if not p:
        await send(update, "❌ Product not found.", reply_markup=back("shop", lang))
        return
    variants = get_variants(pid)
    desc = md(p["description"]) if p["description"] else t(lang, "no_desc")
    hint = t(lang, "select_plan") if variants else t(lang, "no_plans")
    text = t(lang, "product_text", name=md(p["name"]), line=LINE, desc=desc, hint=hint)
    await send(update, text, reply_markup=product_kb(pid, variants, lang))


async def screen_orders(update, context):
    uid = update.effective_user.id
    lang = get_lang(uid)
    history = get_history(uid)
    if not history:
        await send(update, t(lang, "keys_empty", line=LINE), reply_markup=back(lang=lang))
        return
    text = t(lang, "keys_title", line=LINE)
    for r in history:
        dt = (r["date"] or "").replace("T", " ")[:16]
        text += t(
            lang, "keys_entry",
            pname=md(r["pname"]), vname=md(r["vname"]),
            stars=r["stars_paid"], date=dt, key=r["key_value"], line=LINE,
        )
    await send(update, text, reply_markup=back(lang=lang))


async def screen_language(update, context):
    lang = get_lang(update.effective_user.id)
    await send(update, t(lang, "lang_title"), reply_markup=language_kb())


# ═══════════════════════════════════════════
# SCREENS — OWNER SIDE (English, admin tool)
# ═══════════════════════════════════════════

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
        f"📦 *{md(p['name'])}*\n{LINE}\n"
        f"Status: {status}\n"
        f"Plans: {len(variants)}\n"
        f"{md(p['description']) if p['description'] else 'No description.'}\n{LINE}"
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
    uname = ("@" + md(u["username"])) if u["username"] else "N/A"
    text = (
        f"👤 *User Info*\n{LINE}\n"
        f"🆔 ID: `{u['user_id']}`\n"
        f"📛 Name: {md(u['first_name']) if u['first_name'] else 'N/A'}\n"
        f"🔗 Username: {uname}\n"
        f"📱 Phone: `{u['phone'] or 'Not verified'}`\n"
        f"📅 Joined: {(u['joined_date'] or '')[:10] or 'N/A'}\n"
        f"🛍️ Total Purchases: {purchases}\n"
        f"Status: {status}\n{LINE}"
    )
    await send(update, text, reply_markup=o_user_action_kb(target_uid, bool(u["banned"])))


async def screen_o_stats(update, context):
    s = shop_stats()
    text = (
        f"📊 *Bot Statistics*\n{LINE}\n"
        f"👥 Total Users: {s['users']}\n"
        f"💳 Total Sales: {s['total_sales']}\n"
        f"💰 Total Revenue: ⭐{s['total_revenue']}\n"
        f"{LINE}\n"
        f"📅 Today Sales: {s['today_sales']}\n"
        f"💰 Today Revenue: ⭐{s['today_revenue']}\n"
        f"{LINE}"
    )
    await send(update, text, reply_markup=back("owner"))


async def screen_o_settings(update, context):
    maint = is_maintenance()
    status = "🔴 ENABLED — shop is closed to customers" if maint else "🟢 DISABLED — shop is open"
    text = (
        f"🔧 *Bot Settings*\n{LINE}\n"
        f"Maintenance Mode: {status}\n{LINE}\n"
        "When enabled, customers who try to buy will see a\n"
        "\"bot is under maintenance\" message instead of an invoice."
    )
    await send(update, text, reply_markup=o_settings_kb(maint))


# ═══════════════════════════════════════════
# COMMANDS
# ═══════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or "", u.first_name or "")
    user = get_user(u.id)
    lang = get_lang(u.id)

    if user and user["banned"]:
        await update.message.reply_text(t(lang, "banned"))
        return

    if not is_owner(u.id) and (not user or not user["verified"]):
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton(t(lang, "share_number"), request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text(
            t(lang, "verify_prompt", line=LINE),
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
    lang = get_lang(u.id)
    await update.message.reply_text(
        t(lang, "verify_done", line=LINE, phone=contact.phone_number),
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.MARKDOWN,
    )
    await screen_menu(update, context)


# ═══════════════════════════════════════════
# STARS PAYMENT
# ═══════════════════════════════════════════

async def do_send_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, vid: int):
    uid = update.effective_user.id
    lang = get_lang(uid)

    if is_maintenance() and not is_owner(uid):
        if update.callback_query:
            await safe_answer(update.callback_query, t(lang, "maintenance"), show_alert=True)
        return

    v = get_variant(vid)
    if not v:
        if update.callback_query:
            await safe_answer(update.callback_query, "❌ Not found.", show_alert=True)
        return
    p = get_product(v["product_id"])
    if stock_count(vid) == 0:
        if update.callback_query:
            await safe_answer(update.callback_query, t(lang, "out_of_stock"), show_alert=True)
        return

    await context.bot.send_invoice(
        chat_id=uid,
        title=f"{p['name']} — {v['name']}",
        description=p["description"] or p["name"],
        payload=f"v_{vid}",
        provider_token="",       # empty string = Telegram Stars
        currency="XTR",
        prices=[LabeledPrice(v["name"], v["price_stars"])],
    )
    if update.callback_query:
        await safe_answer(update.callback_query, t(lang, "invoice_sent"))


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    if not q.invoice_payload.startswith("v_"):
        await q.answer(ok=False, error_message="Invalid payment.")
        return
    if is_maintenance() and not is_owner(update.effective_user.id):
        await q.answer(ok=False, error_message="Bot is under maintenance. Please try again later.")
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
    lang  = get_lang(uid)
    now   = datetime.utcnow()

    v   = get_variant(vid)
    p   = get_product(v["product_id"])
    key = pop_key(vid, uid)

    if not key:
        await update.message.reply_text(t(lang, "stockout"), parse_mode=ParseMode.MARKDOWN)
        return

    log_tx(uid, v["product_id"], vid, key, stars)

    await update.message.reply_text(
        t(
            lang, "purchase_complete",
            line=LINE, pname=md(p["name"]), vname=md(v["name"]),
            stars=stars, date=now.strftime("%Y-%m-%d %H:%M UTC"), key=key,
        ),
        parse_mode=ParseMode.MARKDOWN,
    )

    # Notify owner
    if OWNER_ID:
        u = update.effective_user
        buyer_name = md(f"@{u.username}") if u.username else md(u.first_name or str(uid))
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    f"⭐ *New Sale!*\n{LINE}\n"
                    f"👤 Buyer: {buyer_name} (`{uid}`)\n"
                    f"✨ {md(p['name'])} — {md(v['name'])}\n"
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
# BROADCAST
# ═══════════════════════════════════════════

async def broadcast_message(context: ContextTypes.DEFAULT_TYPE, text: str):
    users = all_users()
    sent = failed = 0
    body = f"📢 Announcement\n{LINE}\n{text}"
    for u in users:
        try:
            await context.bot.send_message(chat_id=u["user_id"], text=body)
            sent += 1
        except (Forbidden, BadRequest):
            failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.04)   # stay well under Telegram's rate limits
    return sent, failed


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

    # ── Add product → auto-chains into plan / price / keys ──
    if awaiting == "prod_name":
        if not text:
            await update.message.reply_text("❌ Name can't be empty. Send the *product name*:", parse_mode=ParseMode.MARKDOWN)
            return
        temp["name"] = text
        context.user_data["awaiting"] = "prod_desc"
        await update.message.reply_text(
            '📝 Send the *product description*\n(or type "skip" to leave empty):',
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if awaiting == "prod_desc":
        desc = "" if text.lower() == "skip" else text
        name = temp.get("name", "")
        pid = add_product(name, desc)
        context.user_data["temp"] = {"product_id": pid}
        context.user_data["awaiting"] = "var_name"
        await update.message.reply_text(
            f"✅ Product *{md(name)}* created!\n"
            "Now let's set up its first plan.\n\n"
            "🏷️ Send the *plan name*\n(e.g. '1 Month', 'Basic', 'VIP'):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── Add variant (plan) → auto-chains into price → keys ──
    if awaiting == "var_name":
        if not text:
            await update.message.reply_text("❌ Name can't be empty. Send the *plan name*:", parse_mode=ParseMode.MARKDOWN)
            return
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
        pid = temp["product_id"]
        vid = add_variant(pid, temp["vname"], price)
        context.user_data["temp"] = {"product_id": pid, "variant_id": vid}
        context.user_data["awaiting"] = "stock_keys"
        await update.message.reply_text(
            f"✅ Plan *{md(temp['vname'])}* added at ⭐{price}!\n\n"
            "🔐 Now send the *stock keys* for this plan.\n"
            "One per line or comma-separated:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── Add stock keys → offers to add another plan or finish ──
    if awaiting == "stock_keys":
        raw  = text.replace(",", "\n")
        keys = [k.strip() for k in raw.split("\n") if k.strip()]
        if not keys:
            await update.message.reply_text("❌ No valid keys found. Send at least one key.")
            return
        vid  = temp["variant_id"]
        pid  = temp["product_id"]
        add_stock_keys(vid, keys)
        clear(context)
        v = get_variant(vid)
        await update.message.reply_text(
            f"✅ Added *{len(keys)}* key(s) to *{md(v['name'])}*.\n"
            f"Total in stock: {stock_count(vid)}\n\n"
            "Want to add another plan, or finish here?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=o_next_step_kb(pid),
        )
        return

    # ── Broadcast ──────────────────────────
    if awaiting == "broadcast_msg":
        if not text:
            await update.message.reply_text("❌ Message can't be empty. Send the announcement text:")
            return
        clear(context)
        status_msg = await update.message.reply_text("📣 Sending broadcast…")
        sent, failed = await broadcast_message(context, text)
        await status_msg.edit_text(
            f"✅ *Broadcast finished!*\n{LINE}\n"
            f"📨 Delivered: {sent}\n"
            f"❌ Failed: {failed}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await update.message.reply_text("⚙️ *Owner Panel*", parse_mode=ParseMode.MARKDOWN, reply_markup=owner_kb())
        return


# ═══════════════════════════════════════════
# BUTTON HANDLER
# ═══════════════════════════════════════════

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    d   = q.data
    uid = update.effective_user.id
    lang = get_lang(uid)

    # ── Shared ──────────────────────────────
    if d == "menu":
        await screen_menu(update, context)
        return
    if d == "shop":
        u = get_user(uid)
        if u and u["banned"]:
            await safe_answer(q, t(lang, "banned"), show_alert=True)
            return
        await screen_shop(update, context)
        return
    if d == "orders":
        await screen_orders(update, context)
        return
    if d == "lang_menu":
        await screen_language(update, context)
        return
    if d.startswith("setlang_"):
        code = d.split("_", 1)[1]
        if code in LANGS:
            set_lang(uid, code)
            await safe_answer(q, t(code, "lang_set"))
        else:
            await safe_answer(q)
        await screen_menu(update, context)
        return
    if d == "oos":
        await safe_answer(q, t(lang, "out_of_stock"), show_alert=True)
        return
    if d.startswith("p_"):
        await screen_product(update, context, int(d[2:]))
        return
    if d.startswith("buy_"):
        await do_send_invoice(update, context, int(d[4:]))
        return

    # ── Owner only ───────────────────────────
    if not is_owner(uid):
        await safe_answer(q, "Owner only.", show_alert=True)
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
    if d == "o_settings":
        await screen_o_settings(update, context)
        return
    if d == "o_togglemaint":
        set_maintenance(not is_maintenance())
        await safe_answer(q, "✅ Updated.")
        await screen_o_settings(update, context)
        return
    if d == "o_broadcast":
        context.user_data.update(awaiting="broadcast_msg", temp={})
        await safe_answer(q)
        await q.message.reply_text(
            "📣 Send the *announcement message* (text or link)\nyou want to broadcast to all users:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Product management
    if d.startswith("op_"):
        await screen_o_product(update, context, int(d[3:]))
        return
    if d == "o_addprod":
        context.user_data.update(awaiting="prod_name", temp={})
        await safe_answer(q)
        await q.message.reply_text("📦 Send the *product name*:", parse_mode=ParseMode.MARKDOWN)
        return
    if d.startswith("o_addvar_"):
        pid = int(d.split("_")[2])
        context.user_data.update(awaiting="var_name", temp={"product_id": pid})
        await safe_answer(q)
        await q.message.reply_text(
            "🏷️ Send the *plan name*\n(e.g. '1 Month', 'Basic', 'VIP'):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if d.startswith("ov_"):
        vid = int(d[3:])
        v   = get_variant(vid)
        if not v:
            await send(update, "❌ Not found.", reply_markup=back("o_products"))
            return
        cnt = stock_count(vid)
        await send(
            update,
            f"📋 *{md(v['name'])}*\n{LINE}\n"
            f"⭐ Price: {v['price_stars']} Stars\n"
            f"📦 Keys in stock: {cnt}\n{LINE}",
            reply_markup=o_variant_kb(vid, v["product_id"]),
        )
        return
    if d.startswith("o_addstock_"):
        vid = int(d.split("_")[2])
        v   = get_variant(vid)
        if not v:
            await safe_answer(q, "❌ Not found.", show_alert=True)
            return
        context.user_data.update(awaiting="stock_keys", temp={"variant_id": vid, "product_id": v["product_id"]})
        await safe_answer(q)
        await q.message.reply_text(
            "🔐 Send the *keys* for this plan.\nOne per line or comma-separated:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if d.startswith("o_delvar_"):
        parts = d.split("_")
        vid, pid = int(parts[2]), int(parts[3])
        toggle_variant(vid, False)
        await safe_answer(q, "🗑️ Plan deleted.")
        await screen_o_product(update, context, pid)
        return
    if d.startswith("o_delprod_"):
        pid = int(d.split("_")[2])
        toggle_product(pid, False)
        await safe_answer(q, "🗑️ Product deleted.")
        await screen_o_products(update, context)
        return

    # User management
    if d.startswith("ou_"):
        await screen_o_user(update, context, int(d[3:]))
        return
    if d.startswith("o_ban_"):
        target = int(d.split("_")[2])
        set_banned(target, True)
        await safe_answer(q, "🚫 User banned.")
        await screen_o_user(update, context, target)
        return
    if d.startswith("o_unban_"):
        target = int(d.split("_")[2])
        set_banned(target, False)
        await safe_answer(q, "✅ User unbanned.")
        await screen_o_user(update, context, target)
        return

    await safe_answer(q)


# ═══════════════════════════════════════════
# GLOBAL ERROR HANDLER
# (prevents a single bad message/click from "freezing" the bot)
# ═══════════════════════════════════════════

async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    try:
        if isinstance(update, Update):
            if context.user_data is not None:
                context.user_data.pop("awaiting", None)
                context.user_data.pop("temp", None)
            if update.effective_message:
                await update.effective_message.reply_text(
                    "⚠️ Something went wrong. Please try again.\n"
                    "If you were in the middle of something, start over from the menu (/start)."
                )
            elif update.callback_query:
                await safe_answer(update.callback_query, "⚠️ Something went wrong, please try again.", show_alert=True)
    except Exception:
        logger.exception("Error inside error_handler itself.")


# ═══════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set.")
    logger.info(f"Stars Shop Bot starting | OWNER_ID={OWNER_ID}")
    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)   # process updates in parallel → snappier under load
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)

    logger.info("Polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
