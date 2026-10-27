import logging
import mimetypes
import os
import secrets
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.helpers import escape_markdown

# --------------------------------------------------------------------------- #
# Configuration & State
# --------------------------------------------------------------------------- #
BOT_TOKEN = "8970344934:AAEOiFICNrumYwxvZD8tMDx6_tqe0xlewA0"
ADMIN_CHAT_ID = 8989172975  # আপনার এডমিন আইডি

API_KEYS = {"titankey_secret_999"}
DEFAULT_WELCOME = (
    "*📸 Titan Image API Bot*\n\n"
    "যেকোনো ছবি বা ইমেজ ফাইল পাঠান এবং সাথে সাথে ডিরেক্ট লিংক পান।"
)
CURRENT_WELCOME_MSG = DEFAULT_WELCOME
REQUIRED_CHANNEL = ""  # চ্যানেল বাধ্যবাধকতা রাখতে চাইলে এখানে ইউজারনেম দিন (যেমন: "@yourchannel")

PUBLIC_BASE_URL = "https://mediahost.onrender.com".rstrip("/")  
MAX_UPLOAD_MB = 20        
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

ALLOWED_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/tiff"}

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("imagehost")
logging.getLogger("httpx").setLevel(logging.WARNING)


def build_direct_url(file_path: str) -> str:
    if file_path.startswith("http"):
        return file_path
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"


def sniff_image(data: bytes) -> bool:
    return (
        data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"\x89PNG\r\n\x1a\n")
        or data[:6] in (b"GIF87a", b"GIF89a")
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        or data.startswith(b"BM")
        or data[:4] in (b"II*\x00", b"MM\x00*")
    )


# --------------------------------------------------------------------------- #
# Bot Handlers & Admin Keyboard
# --------------------------------------------------------------------------- #
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    is_admin = (user_id == ADMIN_CHAT_ID)

    # এডমিন হলে নিচের স্থায়ী কিবোর্ড বাটন দেখাবে
    reply_markup = None
    if is_admin:
        keyboard = [
            [KeyboardButton("🔑 Gen API Key"), KeyboardButton("📊 Bot Stats")],
            [KeyboardButton("ℹ️ Help / Info")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    api_base = PUBLIC_BASE_URL or "https://YOUR_SERVER"
    text = CURRENT_WELCOME_MSG + f"\n\n🔗 সার্ভার লিংক: `{api_base}`"
    
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)


# এডমিন কিবোর্ড বাটন হ্যান্ডলার
async def handle_admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != ADMIN_CHAT_ID:
        return

    text = update.message.text

    if text == "🔑 Gen API Key":
        new_key = f"titan_{secrets.token_hex(16)}"
        API_KEYS.add(new_key)
        await update.message.reply_text(f"✅ নতুন এপিআই কি তৈরি করা হয়েছে:\n\n`{new_key}`", parse_mode=ParseMode.MARKDOWN)

    elif text == "📊 Bot Stats":
        await update.message.reply_text(
            f"📊 *বট স্ট্যাটাস*\n\n"
            f"• মোট একটিভ এপিআই কি: `{len(API_KEYS)}`\n"
            f"• সার্ভার লিংক: `{PUBLIC_BASE_URL}`",
            parse_mode=ParseMode.MARKDOWN
        )

    elif text == "ℹ️ Help / Info":
        await update.message.reply_text(
            "🛠️ *এডমিন গাইড*\n\n"
            "• ছবি পাঠালে সরাসরি লিংক পাবেন।\n"
            "• কিবোর্ড থেকে '🔑 Gen API Key' এ ক্লিক করে নতুন কি বানাতে পারবেন।\n"
            "• ওয়েলকাম মেসেজ বদলাতে `/setwelcome [আপনার মেসেজ]` লিখুন।",
            parse_mode=ParseMode.MARKDOWN
        )


async def set_welcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global CURRENT_WELCOME_MSG
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    new_msg = " ".join(context.args)
    if not new_msg:
        await update.message.reply_text("⚠️ ব্যবহার: `/setwelcome আপনার নতুন মেসেজ`", parse_mode=ParseMode.MARKDOWN)
        return
    CURRENT_WELCOME_MSG = new_msg
    await update.message.reply_text("✅ ওয়েলকাম মেসেজ আপডেট করা হয়েছে!")


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    status_msg = None
    try:
        if msg.photo:
            tg_file_obj = msg.photo[-1]
        elif msg.document and (msg.document.mime_type or "") in ALLOWED_MIME:
            tg_file_obj = msg.document
        else:
            await msg.reply_text("⚠️ অনুগ্রহ করে একটি সঠিক ছবি (JPEG, PNG, WebP) পাঠান।")
            return

        # লোডিং মেসেজ
        status_msg = await msg.reply_text("⏳ ছবি প্রসেস করা হচ্ছে...")

        # টেলিগ্রাম ফাইল ইনফো ফেচ করা
        file = await context.bot.get_file(tg_file_obj.file_id)
        direct_url = build_direct_url(file.file_path)
        proxy_link = f"{PUBLIC_BASE_URL}/img/{tg_file_obj.file_id}" if PUBLIC_BASE_URL else direct_url

        # লিংক পাঠানো
        await msg.reply_text(f"`{proxy_link}`", parse_mode=ParseMode.MARKDOWN)

        # প্রসেসিং মেসেজ রিমুভ
        if status_msg:
            await status_msg.delete()

    except Exception as e:
        logger.exception("Image processing error: %s", e)
        if status_msg:
            try:
                await status_msg.delete()
            except:
                pass
        await msg.reply_text("❌ ছবি প্রসেস করার সময় একটু সমস্যা হয়েছিল। দয়া করে আবার ছবিটি পাঠান।")


async def bot_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled bot error", exc_info=context.error)


# --------------------------------------------------------------------------- #
# FastAPI Lifespan & App
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    tg_app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(30)
        .write_timeout(60)
        .connect_timeout(15)
        .build()
    )
    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CommandHandler("setwelcome", set_welcome_cmd))
    
    # এডমিন কিবোর্ড বাটন টেক্সট হ্যান্ডলার
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_buttons))
    
    # ইমেজ হ্যান্ডলার
    tg_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image))
    tg_app.add_error_handler(bot_error_handler)

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    app.state.tg_app = tg_app
    app.state.admin_chat_id = ADMIN_CHAT_ID
    logger.info("Fixed Master Bot started successfully.")

    try:
        yield
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()


app = FastAPI(title="Telegram Image Hosting API", version="2.1.0", lifespan=lifespan)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(key: Optional[str] = Depends(api_key_header)) -> str:
    if key and any(secrets.compare_digest(key, valid) for valid in API_KEYS):
        return key
    raise HTTPException(status_code=403, detail="Unauthorized")

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/api/v1/upload-image")
async def upload_image(request: Request, file: UploadFile = File(...), _: str = Depends(verify_api_key)):
    bot = request.app.state.tg_app.bot
    chat_id = request.app.state.admin_chat_id

    if (file.content_type or "") not in ALLOWED_MIME:
        raise HTTPException(status_code=415, detail="Unsupported content type")

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if not data or len(data) > MAX_UPLOAD_BYTES or not sniff_image(data):
        raise HTTPException(status_code=400, detail="Invalid image file")

    filename = os.path.basename(file.filename or "image") or "image"
    sent = await bot.send_document(chat_id=chat_id, document=InputFile(data, filename=filename), disable_notification=True)
    file_id = sent.document.file_id
    tg_file = await bot.get_file(file_id)
    direct_link = build_direct_url(tg_file.file_path)

    return JSONResponse({
        "status": "success",
        "filename": filename,
        "direct_link": direct_link,
        "proxy_link": f"{PUBLIC_BASE_URL}/img/{file_id}" if PUBLIC_BASE_URL else direct_link,
        "file_id": file_id,
    })

@app.get("/img/{file_id}")
async def serve_image(request: Request, file_id: str):
    bot = request.app.state.tg_app.bot
    tg_file = await bot.get_file(file_id)
    content = await tg_file.download_as_bytearray()
    media_type = mimetypes.guess_type(tg_file.file_path)[0] or "application/octet-stream"
    return Response(content=bytes(content), media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})
