import asyncio
import io
import re
import os
import sqlite3
from datetime import datetime
from io import BytesIO
from PIL import Image, ImageDraw
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    Application
)
from playwright.async_api import async_playwright
import imageio
from fastapi import FastAPI
from contextlib import asynccontextmanager
import uvicorn
import numpy as np

TOKEN = os.environ.get("BOT_TOKEN", "478454887:O-jcRMDoEF6QtaKObV5IVLOKc8asaY3ceys")
BALE_BASE_URL = "https://tapi.bale.ai/bot"

ADMIN_ID = 1826980748
NOT_ADMIN_TEXT = "⛔ شما اجازه دسترسی به پنل ادمین را ندارید."
DB_PATH = "bot_data.db"

user_sessions = {}
admin_states = {}

if not os.path.exists("videos"):
    os.makedirs("videos")

# ========== پایگاه داده (توابع همگام) ==========
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_seen TIMESTAMP,
                    last_seen TIMESTAMP
                )''')
    for col, col_type in [("first_name", "TEXT"), ("last_name", "TEXT"), ("username", "TEXT")]:
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    c.execute('''CREATE TABLE IF NOT EXISTS visits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    url TEXT,
                    visited_at TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS banned_users (
                    user_id INTEGER PRIMARY KEY,
                    banned_at TIMESTAMP
                )''')
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def add_user_sync(user_id: int, user=None):
    conn = get_db()
    c = conn.cursor()
    now = datetime.now()
    first_name = user.first_name if user else None
    last_name = user.last_name if user else None
    username = user.username if user else None
    c.execute("""
        INSERT INTO users (user_id, first_seen, last_seen, first_name, last_name, username)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            last_seen = ?,
            first_name = COALESCE(?, first_name),
            last_name = COALESCE(?, last_name),
            username = COALESCE(?, username)
    """, (user_id, now, now, first_name, last_name, username,
          now, first_name, last_name, username))
    conn.commit()
    conn.close()

def add_visit_sync(user_id: int, url: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO visits (user_id, url, visited_at) VALUES (?, ?, ?)",
              (user_id, url, datetime.now()))
    conn.commit()
    conn.close()

def get_all_users_sync():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id, first_name, last_name, username FROM users ORDER BY user_id")
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_visits_sync():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id, url, visited_at FROM visits ORDER BY visited_at DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def is_user_banned_sync(user_id: int) -> bool:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def ban_user_sync(user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO banned_users (user_id, banned_at) VALUES (?, ?)",
              (user_id, datetime.now()))
    conn.commit()
    conn.close()

def unban_user_sync(user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_banned_users_sync():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT b.user_id, u.first_name, u.last_name, u.username
        FROM banned_users b
        LEFT JOIN users u ON b.user_id = u.user_id
        ORDER BY b.banned_at DESC
    """)
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_visits_with_users_sync():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT v.user_id, v.url, v.visited_at,
               u.first_name, u.last_name, u.username
        FROM visits v
        LEFT JOIN users u ON v.user_id = u.user_id
        ORDER BY v.visited_at DESC
    """)
    rows = c.fetchall()
    conn.close()
    return rows

# ========== پوشش async برای دیتابیس ==========
async def add_user(user_id, user=None):
    return await asyncio.to_thread(add_user_sync, user_id, user)

async def add_visit(user_id, url):
    return await asyncio.to_thread(add_visit_sync, user_id, url)

async def get_all_users():
    return await asyncio.to_thread(get_all_users_sync)

async def get_all_visits():
    return await asyncio.to_thread(get_all_visits_sync)

async def is_user_banned(user_id):
    return await asyncio.to_thread(is_user_banned_sync, user_id)

async def ban_user(user_id):
    return await asyncio.to_thread(ban_user_sync, user_id)

async def unban_user(user_id):
    return await asyncio.to_thread(unban_user_sync, user_id)

async def get_banned_users():
    return await asyncio.to_thread(get_banned_users_sync)

async def get_all_visits_with_users():
    return await asyncio.to_thread(get_all_visits_with_users_sync)

# ========== توابع کمکی ==========
def main_keyboard(is_mobile=False):
    device_btn = "📱 موبایل" if not is_mobile else "💻 دسکتاپ"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬆️ بالا", callback_data="scroll_up"),
            InlineKeyboardButton("🔄 رفرش", callback_data="refresh"),
            InlineKeyboardButton("⬇️ پایین", callback_data="scroll_down")
        ],
        [
            InlineKeyboardButton("🎯 کلیک هوشمند", callback_data="smart_click"),
            InlineKeyboardButton("📍 کلیک مختصات", callback_data="coord_click")
        ],
        [
            InlineKeyboardButton("⌨️ تایپ با مختصات", callback_data="type"),
            InlineKeyboardButton("📹 رکورد ویدیو", callback_data="record_video")
        ],
        [
            InlineKeyboardButton("🔲 اسکرین کامل", callback_data="full_screenshot"),
            InlineKeyboardButton("📄 خروجی PDF", callback_data="pdf")
        ],
        [
            InlineKeyboardButton("📥 استخراج متن", callback_data="scrape"),
            InlineKeyboardButton("💾 بایگانی", callback_data="wayback")
        ],
        [
            InlineKeyboardButton(device_btn, callback_data="toggle_device"),
            InlineKeyboardButton("❌ بستن", callback_data="close")
        ]
    ])

def screenshot_to_jpeg(screenshot_bytes, quality=85):
    img = Image.open(BytesIO(screenshot_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    out = BytesIO()
    img.save(out, format="JPEG", quality=quality)
    return out.getvalue()

def draw_grid_on_image(image_bytes, step=100):
    image = Image.open(io.BytesIO(image_bytes))
    if image.mode in ("RGBA", "P"):
        image = image.convert("RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    for x in range(0, width, step):
        draw.line([(x, 0), (x, height)], fill="red", width=1)
        draw.text((x + 2, 5), str(x), fill="red")
    for y in range(0, height, step):
        draw.line([(0, y), (width, y)], fill="red", width=1)
        if y != 0:
            draw.text((5, y + 2), str(y), fill="red")
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=85)
    return output.getvalue()

SMART_CLICK_JS = """
() => {
    let count = 0;
    window.__smart_elements = {};
    let selector = 'a, button, input, textarea, select, [role="button"], [role="textbox"], [contenteditable="true"]';
    document.querySelectorAll(selector).forEach(el => {
        let rect = el.getBoundingClientRect();
        if(rect.width === 0 || rect.height === 0 || el.style.visibility === 'hidden' || el.style.display === 'none') return;
        count++;
        el.style.border = '2px solid red';
        let label = document.createElement('div');
        label.innerText = count;
        label.style.position = 'absolute';
        label.style.left = (rect.left + window.scrollX) + 'px';
        label.style.top = (rect.top + window.scrollY) + 'px';
        label.style.background = 'yellow';
        label.style.color = 'black';
        label.style.fontWeight = 'bold';
        label.style.zIndex = 10000;
        label.style.padding = '2px';
        label.style.fontSize = '14px';
        document.body.appendChild(label);
        window.__smart_elements[count] = {x: rect.left + rect.width/2, y: rect.top + rect.height/2};
    });
    return window.__smart_elements;
}
"""

async def send_current_view(query_or_message, session, caption="✅ وضعیت صفحه:"):
    raw_bytes = await session["page"].screenshot(full_page=False)
    jpeg_bytes = screenshot_to_jpeg(raw_bytes)
    photo_file = InputFile(BytesIO(jpeg_bytes), filename="screenshot.jpg")
    markup = main_keyboard(session["is_mobile"])
    if hasattr(query_or_message, 'message') and query_or_message.message is not None:
        await query_or_message.message.reply_photo(photo=photo_file, caption=caption, reply_markup=markup)
    else:
        await query_or_message.reply_photo(photo=photo_file, caption=caption, reply_markup=markup)

async def track_user(update: Update):
    user = update.effective_user
    if user:
        await add_user(user.id, user)

# ========== هندلرهای عمومی ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID and await is_user_banned(user_id):
        return
    await track_user(update)
    await update.message.reply_text("👋 خوش آمدید! لینک سایت مورد نظر را بفرستید.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID and await is_user_banned(user_id):
        return

    text = update.message.text
    if user_id == ADMIN_ID and user_id in admin_states:
        await handle_admin_input(update, context)
        return

    await track_user(update)
    if user_id in user_sessions and user_sessions[user_id].get("expected_action"):
        await handle_action_input(update, context)
        return

    if re.match(r'^https?://', text):
        await load_url(update.message, context.bot_data['pw'], context.bot_data['browser'], text, user_id)
    else:
        await update.message.reply_text("⚠️ یک لینک معتبر بفرستید.")

async def load_url(message, pw, browser, url, user_id, is_mobile=False):
    processing_msg = await message.reply_text("⏳ در حال بارگذاری...")
    new_context = None
    try:
        if user_id in user_sessions and "browser_context" in user_sessions[user_id]:
            await user_sessions[user_id]["browser_context"].close()

        context_options = {
            "viewport": {"width": 1280, "height": 720},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "ignore_https_errors": True,
            "bypass_csp": True,
        }
        if is_mobile:
            context_options = pw.devices['iPhone 13']
            context_options.update({"ignore_https_errors": True, "bypass_csp": True})

        new_context = await browser.new_context(**context_options)
        page = await new_context.new_page()
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {} };
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) =>
                parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters);
        """)
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")

        user_sessions[user_id] = {
            "browser_context": new_context,
            "page": page,
            "url": url,
            "is_mobile": is_mobile,
            "expected_action": None,
            "smart_elements": {}
        }
        await add_user(user_id, message.from_user)
        await add_visit(user_id, url)
        await send_current_view(message, user_sessions[user_id], f"✅ لود شد:\n🔗 {url}")
        await processing_msg.delete()
    except Exception as e:
        if new_context:
            await new_context.close()
        await processing_msg.edit_text(f"❌ خطا: {e}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != ADMIN_ID and await is_user_banned(user_id):
        try:
            await query.answer()
        except:
            pass
        return

    try:
        await query.answer()
    except:
        pass

    if user_id not in user_sessions:
        try:
            await query.edit_message_caption(caption="⚠️ صفحه فعالی ندارید.")
        except:
            pass
        return

    action = query.data
    session = user_sessions[user_id]
    page = session["page"]
    try:
        if action == "scroll_up":
            await page.evaluate("window.scrollBy(0, -600)")
            await send_current_view(query, session, "⬆️ اسکرول بالا")
        elif action == "scroll_down":
            await page.evaluate("window.scrollBy(0, 600)")
            await send_current_view(query, session, "⬇️ اسکرول پایین")
        elif action == "refresh":
            await page.reload(wait_until="domcontentloaded")
            await send_current_view(query, session, "🔄 رفرش شد")
        elif action == "full_screenshot":
            msg = await query.message.reply_text("⏳ گرفتن اسکرین‌شات...")
            full_pic = await page.screenshot(full_page=True)
            file = InputFile(BytesIO(screenshot_to_jpeg(full_pic, quality=80)), filename="full.jpg")
            await query.message.reply_document(document=file, filename="full.jpg")
            await msg.delete()
        elif action == "pdf":
            msg = await query.message.reply_text("⏳ تولید PDF...")
            pdf_bytes = await page.pdf(format="A4")
            file = InputFile(BytesIO(pdf_bytes), filename="page.pdf")
            await query.message.reply_document(document=file, filename="page.pdf")
            await msg.delete()
        elif action == "smart_click":
            elements = await page.evaluate(SMART_CLICK_JS)
            session["smart_elements"] = elements
            session["expected_action"] = "smart_click"
            pic_raw = await page.screenshot(full_page=False)
            pic_jpeg = screenshot_to_jpeg(pic_raw)
            await query.message.reply_photo(
                photo=InputFile(BytesIO(pic_jpeg), filename="smart.jpg"),
                caption="🎯 شماره المان را بفرستید:")
        elif action == "coord_click":
            session["expected_action"] = "coord_click"
            raw_bytes = await page.screenshot(full_page=False)
            grid_image = draw_grid_on_image(raw_bytes)
            await query.message.reply_photo(
                photo=InputFile(BytesIO(grid_image), filename="grid.jpg"),
                caption="📍 مختصات X Y را با فاصله بفرستید (مثال: 400 150)")
        elif action == "type":
            session["expected_action"] = "coord_type"
            raw_bytes = await page.screenshot(full_page=False)
            grid_image = draw_grid_on_image(raw_bytes)
            await query.message.reply_photo(
                photo=InputFile(BytesIO(grid_image), filename="grid.jpg"),
                caption="⌨️ مختصات و متن را بفرستید (مثال: 400 150 سلام)")
        elif action == "record_video":
            session["expected_action"] = "record_video"
            await query.message.reply_text("📹 زمان ویدیو (۵ تا ۶۰ ثانیه) را بفرستید:")
        elif action == "scrape":
            text_content = await page.evaluate("document.body.innerText")
            await query.message.reply_text(f"📄 متن:\n\n{text_content[:4000]}")
        elif action == "wayback":
            await query.message.reply_text("📦 قابلیت بایگانی در حال توسعه است. لطفاً صبور باشید.")
        elif action == "toggle_device":
            is_mob = not session["is_mobile"]
            await load_url(query.message, context.bot_data['pw'], context.bot_data['browser'],
                           session["url"], user_id, is_mobile=is_mob)
        elif action == "close":
            await session["browser_context"].close()
            del user_sessions[user_id]
            await query.edit_message_caption(caption="❌ بسته شد.")
    except Exception as e:
        await query.message.reply_text(f"❌ خطا: {e}")

async def handle_action_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = user_sessions[user_id]
    action = session["expected_action"]
    text = update.message.text
    page = session["page"]

    try:
        if action == "smart_click":
            element_id = text.strip()
            elements = session.get("smart_elements", {})
            if str(element_id) in elements:
                await page.mouse.click(float(elements[element_id]['x']), float(elements[element_id]['y']))
                await asyncio.sleep(2)
                session["expected_action"] = None
                await send_current_view(update, session, "✅ کلیک انجام شد.")
            else:
                await update.message.reply_text("❌ شماره نامعتبر.")
        elif action == "coord_click":
            parts = text.split()
            x, y = float(parts[0]), float(parts[1])
            await page.mouse.click(x, y)
            await asyncio.sleep(2)
            session["expected_action"] = None
            await send_current_view(update, session, f"✅ کلیک در {x},{y}")
        elif action == "coord_type":
            parts = text.split(" ", 2)
            if len(parts) < 3:
                await update.message.reply_text("❌ فرمت نادرست. مثال: 400 150 سلام")
                session["expected_action"] = None
                return
            x, y, val = float(parts[0]), float(parts[1]), parts[2]
            await page.mouse.click(x, y)
            await asyncio.sleep(0.5)
            await page.keyboard.type(val, delay=50)
            await asyncio.sleep(1)
            session["expected_action"] = None
            await send_current_view(update, session, "✅ تایپ شد.")
        elif action == "record_video":
            try:
                duration = int(text.strip())
            except ValueError:
                await update.message.reply_text("❌ لطفاً یک عدد صحیح وارد کنید.")
                session["expected_action"] = None
                return
            if duration < 5 or duration > 60:
                await update.message.reply_text("⏱ مدت زمان باید بین ۵ تا ۶۰ ثانیه باشد.")
                session["expected_action"] = None
                return

            session["expected_action"] = None
            msg = await update.message.reply_text(f"🎥 در حال ضبط {duration} ثانیه از صفحه فعلی...")
            fps = 10
            total_frames = duration * fps
            video_path = f"videos/record_{user_id}_{int(datetime.now().timestamp())}.mp4"
            writer = None
            try:
                writer = imageio.get_writer(
                    video_path, fps=fps, format='FFMPEG',
                    codec='libx264', quality=8, pixelformat='yuv420p'
                )
                for _ in range(total_frames):
                    screenshot = await page.screenshot(full_page=False, type='png')
                    img = Image.open(BytesIO(screenshot))
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    max_width = 1280
                    if img.width > max_width:
                        ratio = max_width / img.width
                        new_height = int(img.height * ratio)
                        img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
                    frame_array = np.array(img)
                    writer.append_data(frame_array)
                    await asyncio.sleep(1 / fps)
                writer.close()
                writer = None
                with open(video_path, 'rb') as f:
                    video_file = InputFile(f, filename="video.mp4")
                    await update.message.reply_video(video=video_file)
                os.remove(video_path)
                await msg.delete()
            except Exception as e:
                if writer is not None:
                    try:
                        writer.close()
                    except:
                        pass
                if os.path.exists(video_path):
                    os.remove(video_path)
                await msg.edit_text(f"❌ خطا در ضبط ویدیو: {e}")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")
        session["expected_action"] = None

# ========== بخش مدیریت ادمین ==========
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text(NOT_ADMIN_TEXT)
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 لیست کاربران", callback_data="admin_users")],
        [InlineKeyboardButton("🌐 لینک‌های بازدید شده", callback_data="admin_sites")],
        [InlineKeyboardButton("🚫 بن کاربر", callback_data="admin_ban")],
        [InlineKeyboardButton("✅ رفع بن", callback_data="admin_unban")],
        [InlineKeyboardButton("📋 لیست بن شدگان", callback_data="admin_banned_list")],
        [InlineKeyboardButton("❌ بستن پنل", callback_data="admin_close")]
    ])
    await update.message.reply_text("🔐 پنل مدیریت:", reply_markup=keyboard)

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(NOT_ADMIN_TEXT)
        return
    await send_users_list(update)

async def sites_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(NOT_ADMIN_TEXT)
        return
    await send_sites_list(update)

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != ADMIN_ID:
        await query.answer(NOT_ADMIN_TEXT, show_alert=True)
        return
    await query.answer()
    data = query.data
    if data == "admin_users":
        await send_users_list(query)
    elif data == "admin_sites":
        await send_sites_list(query)
    elif data == "admin_close":
        await query.edit_message_text("پنل مدیریت بسته شد.")
    elif data == "admin_ban":
        admin_states[user_id] = "awaiting_ban_id"
        await query.message.reply_text("🔢 لطفاً آیدی عددی کاربر برای بن را ارسال کنید:")
    elif data == "admin_unban":
        admin_states[user_id] = "awaiting_unban_id"
        await query.message.reply_text("🔢 لطفاً آیدی عددی کاربر برای رفع بن را ارسال کنید:")
    elif data == "admin_banned_list":
        await send_banned_list(query)

async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = admin_states.get(user_id)
    text = update.message.text.strip()
    if state == "awaiting_ban_id":
        if not text.isdigit():
            await update.message.reply_text("❌ لطفاً فقط عدد وارد کنید.")
            return
        target_id = int(text)
        if target_id == ADMIN_ID:
            await update.message.reply_text("❌ نمی‌توانید خودتان را بن کنید.")
        else:
            await ban_user(target_id)
            await update.message.reply_text(f"✅ کاربر {target_id} بن شد.")
        del admin_states[user_id]
    elif state == "awaiting_unban_id":
        if not text.isdigit():
            await update.message.reply_text("❌ لطفاً فقط عدد وارد کنید.")
            return
        target_id = int(text)
        await unban_user(target_id)
        await update.message.reply_text(f"✅ کاربر {target_id} از بن خارج شد.")
        del admin_states[user_id]

async def send_users_list(target):
    users = await get_all_users()
    if not users:
        text = "ℹ️ هنوز هیچ کاربری از ربات استفاده نکرده است."
        if hasattr(target, 'message'):
            await target.message.reply_text(text)
        else:
            await target.edit_message_text(text)
        return

    lines = []
    for uid, fname, lname, uname in users:
        name_parts = []
        if fname:
            name_parts.append(fname)
        if lname:
            name_parts.append(lname)
        full_name = " ".join(name_parts) if name_parts else "بی‌نام"
        line = f"👤 {full_name}"
        if uname:
            line += f" (@{uname})"
        line += f"  {uid}"
        lines.append(line)

    full_text = "👥 کاربران ثبت‌شده:\n\n" + "\n".join(lines)
    if len(full_text) > 4000:
        file_bytes = BytesIO(full_text.encode('utf-8'))
        if hasattr(target, 'message'):
            await target.message.reply_document(document=InputFile(file_bytes, filename="users.txt"))
        else:
            await target.message.reply_document(document=InputFile(file_bytes, filename="users.txt"))
    else:
        if hasattr(target, 'message'):
            await target.message.reply_text(full_text)
        else:
            await target.edit_message_text(full_text)

async def send_sites_list(target):
    visits = await get_all_visits_with_users()
    if not visits:
        text = "ℹ️ هنوز هیچ سایتی بارگذاری نشده است."
        if hasattr(target, 'message'):
            await target.message.reply_text(text)
        else:
            await target.edit_message_text(text)
        return

    lines = []
    for uid, url, vtime, fname, lname, uname in visits:
        name_parts = []
        if fname:
            name_parts.append(fname)
        if lname:
            name_parts.append(lname)
        full_name = " ".join(name_parts) if name_parts else "بی‌نام"
        try:
            dt = datetime.strptime(vtime, "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            dt = vtime
        user_display = full_name
        if uname:
            user_display += f" (@{uname})"
        lines.append(f"👤 {user_display}  {uid} → {url}  {dt}")

    full_text = "🌐 سایت‌های بازدید شده:\n\n" + "\n".join(lines)
    if len(full_text) > 4000:
        file_bytes = BytesIO(full_text.encode('utf-8'))
        if hasattr(target, 'message'):
            await target.message.reply_document(document=InputFile(file_bytes, filename="sites.txt"))
        else:
            await target.message.reply_document(document=InputFile(file_bytes, filename="sites.txt"))
    else:
        if hasattr(target, 'message'):
            await target.message.reply_text(full_text)
        else:
            await target.edit_message_text(full_text)

async def send_banned_list(target):
    banned = await get_banned_users()
    if not banned:
        text = "ℹ️ هیچ کاربری بن نشده است."
        if hasattr(target, 'message'):
            await target.message.reply_text(text)
        else:
            await target.edit_message_text(text)
        return

    lines = []
    for uid, fname, lname, uname in banned:
        name_parts = []
        if fname:
            name_parts.append(fname)
        if lname:
            name_parts.append(lname)
        full_name = " ".join(name_parts) if name_parts else "بی‌نام"
        line = f"🚫 {full_name}"
        if uname:
            line += f" (@{uname})"
        line += f"  {uid}"
        lines.append(line)

    full_text = "📋 کاربران بن‌شده:\n\n" + "\n".join(lines)
    if len(full_text) > 4000:
        file_bytes = BytesIO(full_text.encode('utf-8'))
        if hasattr(target, 'message'):
            await target.message.reply_document(document=InputFile(file_bytes, filename="banned.txt"))
        else:
            await target.message.reply_document(document=InputFile(file_bytes, filename="banned.txt"))
    else:
        if hasattr(target, 'message'):
            await target.message.reply_text(full_text)
        else:
            await target.edit_message_text(full_text)

# ========== اجرای هماهنگ ==========
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Initializing Playwright and Bot...")
    init_db()

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .base_url(BALE_BASE_URL)
        .connect_timeout(60.0)
        .read_timeout(60.0)
        .write_timeout(60.0)
        .pool_timeout(60.0)
        .build()
    )

    application.bot_data['pw'] = await async_playwright().start()
    application.bot_data['browser'] = await application.bot_data['pw'].chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-http2",
            "--headless=new",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("sites", sites_command))
    application.add_handler(MessageHandler(filters.TEXT, handle_text))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    application.add_handler(CallbackQueryHandler(button_callback))

    await application.initialize()
    await application.updater.start_polling(drop_pending_updates=True)
    await application.start()

    print("--- Bot is fully Online! ---")
    yield

    print("Shutting down...")
    await application.updater.stop()
    await application.stop()
    await application.bot_data['browser'].close()
    await application.bot_data['pw'].stop()

app = FastAPI(lifespan=lifespan)

@app.get("/")
def read_root():
    return {"status": "active", "bot": "running"}

if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=7860)
