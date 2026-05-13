import asyncio
import io
import re
import os
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

from fastapi import FastAPI
from contextlib import asynccontextmanager
import uvicorn

# --- تنظیمات اولیه ---
TOKEN = os.environ.get("BOT_TOKEN", "478454887:O-jcRMDoEF6QtaKObV5IVLOKc8asaY3ceys")
BALE_BASE_URL = "https://tapi.bale.ai/bot"

ADMIN_ID = 1826980748
NOT_ADMIN_TEXT = "بیلاخ داداش ادمین نیستی! ادمین: @unknow_user2"

user_sessions = {}

if not os.path.exists("videos"):
    os.makedirs("videos")

# --- توابع کمکی ---

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
    """تبدیل اسکرین‌شات خام Playwright به JPEG و برگرداندن bytes"""
    img = Image.open(BytesIO(screenshot_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    out = BytesIO()
    img.save(out, format="JPEG", quality=quality)
    return out.getvalue()

def draw_grid_on_image(image_bytes, step=100):
    """کشیدن خطوط مختصات روی تصویر (ورودی bytes خام) و خروجی JPEG"""
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
    """اسکرین‌شات از نمای فعلی می‌گیرد، به JPEG تبدیل می‌کند و می‌فرستد"""
    raw_bytes = await session["page"].screenshot(full_page=False)
    jpeg_bytes = screenshot_to_jpeg(raw_bytes)
    photo_file = InputFile(BytesIO(jpeg_bytes), filename="screenshot.jpg")
    markup = main_keyboard(session["is_mobile"])

    if hasattr(query_or_message, 'message') and query_or_message.message is not None:
        await query_or_message.message.reply_photo(photo=photo_file, caption=caption, reply_markup=markup)
    else:
        await query_or_message.reply_photo(photo=photo_file, caption=caption, reply_markup=markup)

# --- هندلرهای ربات ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 خوش آمدید! لینک سایت مورد نظر را بفرستید.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    if re.match(r'^https?://', text):
        await load_url(update.message, context.bot_data['pw'], context.bot_data['browser'], text, user_id)
    elif user_id in user_sessions and user_sessions[user_id].get("expected_action"):
        await handle_action_input(update, context)
    else:
        await update.message.reply_text("⚠️ یک لینک معتبر بفرستید.")

async def load_url(message, pw, browser, url, user_id, is_mobile=False):
    processing_msg = await message.reply_text("⏳ در حال بارگذاری...")
    try:
        if user_id in user_sessions and "browser_context" in user_sessions[user_id]:
            await user_sessions[user_id]["browser_context"].close()

        context_options = {"viewport": {"width": 1280, "height": 720}}
        if is_mobile:
            context_options = pw.devices['iPhone 13']

        new_context = await browser.new_context(**context_options)
        page = await new_context.new_page()
        await page.goto(url, timeout=60000, wait_until="networkidle")

        user_sessions[user_id] = {
            "browser_context": new_context,
            "page": page,
            "url": url,
            "is_mobile": is_mobile,
            "expected_action": None,
            "smart_elements": {}
        }
        await send_current_view(message, user_sessions[user_id], f"✅ لود شد:\n🔗 {url}")
        await processing_msg.delete()
    except Exception as e:
        await processing_msg.edit_text(f"❌ خطا: {e}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    # پاسخ فوری به callback حتی اگر timeout شده باشد
    try:
        await query.answer()
    except Exception:
        pass

    if user_id not in user_sessions:
        try:
            await query.edit_message_caption(caption="⚠️ صفحه فعالی ندارید.")
        except Exception:
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
            await page.reload(wait_until="networkidle")
            await send_current_view(query, session, "🔄 رفرش شد")

        elif action == "full_screenshot":
            msg = await query.message.reply_text("⏳ گرفتن اسکرین‌شات...")
            full_pic = await page.screenshot(full_page=True)
            # تبدیل به JPEG با InputFile
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
                caption="🎯 شماره المان را بفرستید:"
            )

        elif action == "coord_click":
            session["expected_action"] = "coord_click"
            raw_bytes = await page.screenshot(full_page=False)
            grid_image = draw_grid_on_image(raw_bytes)  # خروجی JPEG bytes
            await query.message.reply_photo(
                photo=InputFile(BytesIO(grid_image), filename="grid.jpg"),
                caption="📍 مختصات X Y را با فاصله بفرستید (مثال: 400 150)"
            )

        elif action == "type":
            session["expected_action"] = "coord_type"
            raw_bytes = await page.screenshot(full_page=False)
            grid_image = draw_grid_on_image(raw_bytes)
            await query.message.reply_photo(
                photo=InputFile(BytesIO(grid_image), filename="grid.jpg"),
                caption="⌨️ مختصات و متن را بفرستید (مثال: 400 150 سلام)"
            )

        elif action == "record_video":
            session["expected_action"] = "record_video"
            await query.message.reply_text("📹 زمان ویدیو (۵ تا ۶۰ ثانیه) را بفرستید:")

        elif action == "scrape":
            text_content = await page.evaluate("document.body.innerText")
            await query.message.reply_text(f"📄 متن:\n\n{text_content[:4000]}")

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
            x, y, val = float(parts[0]), float(parts[1]), parts[2]
            await page.mouse.click(x, y)
            await asyncio.sleep(0.5)
            await page.keyboard.type(val, delay=50)
            await asyncio.sleep(1)
            session["expected_action"] = None
            await send_current_view(update, session, "✅ تایپ شد.")

        elif action == "record_video":
            duration = int(text.strip())
            session["expected_action"] = None
            msg = await update.message.reply_text(f"🎥 ضبط {duration} ثانیه...")
            pw, browser = context.bot_data['pw'], context.bot_data['browser']
            vid_context = await browser.new_context(record_video_dir="videos/")
            vid_page = await vid_context.new_page()
            await vid_page.goto(session["url"])
            await asyncio.sleep(duration)
            await vid_context.close()
            video_path = await vid_page.video.path()
            with open(video_path, 'rb') as f:
                video_file = InputFile(f, filename="video.mp4")
                await update.message.reply_video(video=video_file)
            os.remove(video_path)
            await msg.delete()

    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")
        session["expected_action"] = None

# --- اجرای هماهنگ سرور و ربات ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Initializing Playwright and Bot...")

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
        args=["--no-sandbox", "--disable-setuid-sandbox"]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT, handle_text))
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
