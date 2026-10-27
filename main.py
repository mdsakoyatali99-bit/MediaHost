import logging
import mimetypes
import os
import secrets
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.helpers import escape_markdown

# --------------------------------------------------------------------------- #
# Configuration & Dynamic State
# --------------------------------------------------------------------------- #
BOT_TOKEN = "8970344934:AAEOiFICNrumYwxvZD8tMDx6_tqe0xlewA0"
ADMIN_CHAT_ID = 8989172975  # এডমিন আইডি (ইন্টজার ফরম্যাট)

# ডাইনামিক এপিআই কি ও ওয়েলকাম মেসেজ স্টোরেজ
API_KEYS = {"titankey_secret_999"}
DEFAULT_WELCOME = (
    "*📸 Titan Image API Bot*\n\n"
    "যেকোনো ছবি বা ইমেজ ফাইল পাঠান এবং সাথে সাথে ডিরেক্ট লিংক পান।"
)
CURRENT_WELCOME_MSG = DEFAULT_WELCOME

# ফোর্স সাবস্ক্রিপশন চ্যানেল (যদি বাধ্যতামূলক করতে চান, যেমন: "@your_channel_username")
REQUIRED_CHANNEL = ""  # খালি রাখলে চেক করবে না, চ্যানেল ইউজারনেম দিলে সেটি সাবস্ক্রাইব করা লাগবে

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


def esc(text: str) -> str:
    return escape_markdown(text, version=2)


# --------------------------------------------------------------------------- #
# Helper: Force Subscription Check
# --------------------------------------------------------------------------- #
async def check_user_subscription(bot, user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        if member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            return True
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------- #
# Telegram Bot Handlers & Admin Commands
# --------------------------------------------------------------------------- #
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    
    # ফোর্স সাবস্ক্রিপশন চেক
    if REQUIRED_CHANNEL and not await check_user_subscription(context.bot, user_id):
        keyboard = [[InlineKeyboardButton("📢 চ্যানেলে জয়েন করুন", url=f"https://t.me/{REQUIRED_CHANNEL.replace('@', '')}")],
                    [InlineKeyboardButton("🔄 চেক সাবস্ক্রিপশন", callback_data="check_sub")]]
        await update.message.reply_text(
            "⚠️ এই বট ব্যবহার করতে হলে আমাদের অফিসিয়াল চ্যানেলে অবশ্যই জয়েন করতে হবে!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    api_base = PUBLIC_BASE_URL or "https://YOUR_SERVER"
    curl = (
        f"curl -X POST {api_base}/api/v1/upload-image \\\n"
        '  -H "X-API-Key: titankey_secret_999" \\\n'
        '  -F "file=@photo.jpg"'
    )
    text = CURRENT_WELCOME_MSG + "\n\n*API Integration*\n" + f"```\n{curl}\n```"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


# ১. এডমিন কমান্ড: নতুন এপিআই কি তৈরি করা (/genkey)
async def gen_key_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ এই কমান্ডটি শুধুমাত্র এডমিনের জন্য!")
        return
    
    new_key = f"titan_{secrets.token_hex(16)}"
    API_KEYS.add(new_key)
    await update.message.reply_text(f"✅ নতুন এপিআই কি সফলভাবে তৈরি করা হয়েছে:\n\n`{new_key}`", parse_mode=ParseMode.MARKDOWN_V2)


# ২. এডমিন কমান্ড: ওয়েলকাম মেসেজ পরিবর্তন করা (/setwelcome)
async def set_welcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global CURRENT_WELCOME_MSG
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ এই কমান্ডটি শুধুমাত্র এডমিনের জন্য!")
        return
    
    new_msg = " ".join(context.args)
    if not new_msg:
        await update.message.reply_text("⚠️ দয়া করে নতুন মেসেজটি লিখে দিন। যেমন: `/setwelcome আপনার ওয়েলকাম টেক্সট`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    
    CURRENT_WELCOME_MSG = new_msg
    await update.message.reply_text("✅ ওয়েলকাম মেসেজ সফলভাবে আপডেট করা হয়েছে!")


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    
    # ফোর্স সাবস্ক্রিপশন চেক
    if REQUIRED_CHANNEL and not await check_user_subscription(context.bot, user_id):
        await update.message.reply_text(f"⚠️ ছবি আপলোড করতে হলে প্রথমে আমাদের চ্যানেলে জয়েন করুন: {REQUIRED_CHANNEL}")
        return

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

        # লোডিং এনিমেশন মেসেজ
        status_msg = await msg.reply_text("⏳ *ছবি প্রসেস করা হচ্ছে...*", parse_mode=ParseMode.MARKDOWN_V2)

        # টেলিগ্রাম থেকে ফাইল ফেচ করা
        file = await context.bot.get_file(tg_file_obj.file_id)
        direct_url = build_direct_url(file.file_path)
        
        # প্রক্সি লিংক তৈরি
        proxy_link = f"{PUBLIC_BASE_URL}/img/{tg_file_obj.file_id}" if PUBLIC_BASE_URL else direct_url

        # পরিষ্কার লিংক পাঠানো
        result_text = f"`{proxy_link}`"
        await msg.reply_text(result_text, parse_mode=ParseMode.MARKDOWN_V2)

        # প্রসেসিং মেসেজ অটো-ডিলিট
        if status_msg:
            await status_msg.delete()

    except Exception:
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        logger.exception("Error in handle_image")
        await msg.reply_text("❌ ছবি প্রসেস করার সময় একটি সমস্যা হয়েছে। আবার চেষ্টা করুন।")


async def bot_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled bot error", exc_info=context.error)


# --------------------------------------------------------------------------- #
# FastAPI App + Lifespan
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
    tg_app.add_handler(CommandHandler("genkey", gen_key_cmd))
    tg_app.add_handler(CommandHandler("setwelcome", set_welcome_cmd))
    tg_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image))
    tg_app.add_error_handler(bot_error_handler)

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    app.state.tg_app = tg_app
    app.state.admin_chat_id = ADMIN_CHAT_ID
    logger.info("Master Bot polling started; API ready.")

    try:
        yield
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
        logger.info("Bot stopped.")


app = FastAPI(title="Telegram Master Image Hosting API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# API Key Authentication & Routes
# --------------------------------------------------------------------------- #
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(key: Optional[str] = Depends(api_key_header)) -> str:
    if key and any(secrets.compare_digest(key, valid) for valid in API_KEYS):
        return key
    raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing API key")

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/api/v1/upload-image")
async def upload_image(request: Request, file: UploadFile = File(...), _: str = Depends(verify_api_key)):
    bot = request.app.state.tg_app.bot
    chat_id = request.app.state.admin_chat_id

    if (file.content_type or "") not in ALLOWED_MIME:
        raise HTTPException(status_code=415, detail=f"Unsupported content type: {file.content_type}")

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large")
    if not sniff_image(data):
        raise HTTPException(status_code=415, detail="Invalid image content")

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
