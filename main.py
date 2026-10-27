import logging
import mimetypes
import os
import secrets
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from telegram import InputFile, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.helpers import escape_markdown

# --------------------------------------------------------------------------- #
# Configuration (আপনার তথ্যগুলো এখানে সরাসরি সেট করা আছে)
# --------------------------------------------------------------------------- #
BOT_TOKEN = "8970344934:AAEOiFICNrumYwxvZD8tMDx6_tqe0xlewA0"
ADMIN_CHAT_ID_RAW = "8989172975"           # আপনার টেলিগ্রাম চ্যাট আইডি
API_KEYS = {"titankey_secret_999"}          # এপিআই কি (অন্যান্য অ্যাপ থেকে আপলোড করার সময় এটি কাজে লাগবে)
PUBLIC_BASE_URL = "https://mediahost.onrender.com".rstrip("/")  # আপনার রেন্ডার লাইভ লিংক
MAX_UPLOAD_MB = 20        # Bot API getFile limit is 20 MB
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

ALLOWED_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/tiff"}

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("imagehost")
logging.getLogger("httpx").setLevel(logging.WARNING)


def _parse_chat_id(raw: str):
    try:
        return int(raw)
    except ValueError:
        return raw


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
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
# Telegram bot handlers
# --------------------------------------------------------------------------- #
def welcome_text() -> str:
    api_base = PUBLIC_BASE_URL or "https://YOUR_SERVER"
    curl = (
        f"curl -X POST {api_base}/api/v1/upload-image \\\n"
        '  -H "X-API-Key: titankey_secret_999" \\\n'
        '  -F "file=@photo.jpg"'
    )
    return (
        "*📸 Image Hosting Bot*\n\n"
        + esc("Send me any photo or image file and I'll instantly give you a direct image link and its file_id.")
        + "\n\n*How to use the bot*\n"
        + esc("1. Send a photo (or an image as a file).") + "\n"
        + esc("2. Copy the Direct URL from my reply.") + "\n\n"
        + "*API integration*\n"
        + esc("Upload from your own apps with a POST request (multipart/form-data, field name 'file') "
              "and an X-API-Key header:")
        + f"\n```\n{curl}\n```\n"
        + esc("The response is JSON: status, filename, direct_link, file_id.")
    )


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(welcome_text(), parse_mode=ParseMode.MARKDOWN_V2)


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    try:
        if msg.photo:
            tg_file_obj = msg.photo[-1]
        elif msg.document and (msg.document.mime_type or "") in ALLOWED_MIME:
            tg_file_obj = msg.document
        else:
            await msg.reply_text("⚠️ Please send a valid image (JPEG, PNG, GIF, WebP...).")
            return

        file = await context.bot.get_file(tg_file_obj.file_id)
        url = build_direct_url(file.file_path)

        text = (
            "✅ *Image uploaded*\n\n"
            f"*Direct URL:*\n`{url}`\n\n"
            f"*File ID:*\n`{tg_file_obj.file_id}`"
        )
        await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)

    except RetryAfter as e:
        await msg.reply_text(f"⏳ Rate limited by Telegram. Retry in {int(e.retry_after)}s.")
    except BadRequest as e:
        logger.warning("BadRequest: %s", e)
        await msg.reply_text("❌ Telegram rejected this file (it may be larger than 20 MB).")
    except (TimedOut, NetworkError):
        await msg.reply_text("🌐 Network problem talking to Telegram. Please try again.")
    except Exception:
        logger.exception("Unexpected error in handle_image")
        await msg.reply_text("❌ Unexpected error. Please try again later.")


async def bot_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled bot error", exc_info=context.error)


# --------------------------------------------------------------------------- #
# FastAPI app + lifespan
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
    tg_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image))
    tg_app.add_error_handler(bot_error_handler)

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    app.state.tg_app = tg_app
    app.state.admin_chat_id = _parse_chat_id(ADMIN_CHAT_ID_RAW)
    logger.info("Bot polling started; API ready.")

    try:
        yield
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
        logger.info("Bot stopped.")


app = FastAPI(title="Telegram Image Hosting API", version="1.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# API key authentication
# --------------------------------------------------------------------------- #
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(key: Optional[str] = Depends(api_key_header)) -> str:
    if key and any(secrets.compare_digest(key, valid) for valid in API_KEYS):
        return key
    raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing API key")


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
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
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_UPLOAD_MB} MB)")
    if not sniff_image(data):
        raise HTTPException(status_code=415, detail="File content is not a recognised image")

    filename = os.path.basename(file.filename or "image") or "image"

    try:
        sent = await bot.send_document(
            chat_id=chat_id,
            document=InputFile(data, filename=filename),
            disable_notification=True,
        )
        file_id = sent.document.file_id
        tg_file = await bot.get_file(file_id)
        direct_link = build_direct_url(tg_file.file_path)

    except RetryAfter as e:
        raise HTTPException(
            status_code=429,
            detail="Telegram rate limit hit",
            headers={"Retry-After": str(int(e.retry_after))},
        )
    except TimedOut:
        raise HTTPException(status_code=504, detail="Telegram request timed out")
    except NetworkError:
        raise HTTPException(status_code=503, detail="Network error contacting Telegram")
    except BadRequest as e:
        logger.warning("Telegram BadRequest: %s", e)
        raise HTTPException(status_code=400, detail=f"Telegram rejected the request: {e.message}")
    except TelegramError:
        logger.exception("Telegram API error")
        raise HTTPException(status_code=502, detail="Telegram API error")
    except Exception:
        logger.exception("Unexpected upload error")
        raise HTTPException(status_code=500, detail="Internal server error")

    response = {
        "status": "success",
        "filename": filename,
        "direct_link": direct_link,
        "file_id": file_id,
    }
    if PUBLIC_BASE_URL:
        response["proxy_link"] = f"{PUBLIC_BASE_URL}/img/{file_id}"
    return JSONResponse(response)


@app.get("/img/{file_id}")
async def serve_image(request: Request, file_id: str):
    bot = request.app.state.tg_app.bot
    try:
        tg_file = await bot.get_file(file_id)
        content = await tg_file.download_as_bytearray()
    except BadRequest:
        raise HTTPException(status_code=404, detail="File not found")
    except RetryAfter as e:
        raise HTTPException(status_code=429, detail="Rate limited", headers={"Retry-After": str(int(e.retry_after))})
    except (TimedOut, NetworkError):
        raise HTTPException(status_code=503, detail="Telegram unreachable")
    except Exception:
        logger.exception("Proxy error")
        raise HTTPException(status_code=500, detail="Internal server error")

    media_type = mimetypes.guess_type(tg_file.file_path)[0] or "application/octet-stream"
    return Response(
        content=bytes(content),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
)
