# created by rajnish sahani

import asyncio
import base64
import hashlib
import logging
import signal
import sys
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = (os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
IG_COOKIES_B64 = os.environ.get("IG_COOKIES_B64", "").strip()
ALLOWED_USER_IDS_RAW = os.environ.get("ALLOWED_USER_IDS", "").strip()
def positive_int(name: str, default: int, maximum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1 or (maximum is not None and value > maximum):
        raise ValueError(f"{name} must be between 1 and {maximum or 'a positive integer'}")
    return value


MAX_UPLOAD_MB = positive_int("MAX_UPLOAD_MB", 49, 49)
MAX_TEMP_MB = positive_int("MAX_TEMP_MB", 256)
MAX_FILES = positive_int("MAX_FILES", 20, 100)
DOWNLOAD_TIMEOUT_SECONDS = positive_int("DOWNLOAD_TIMEOUT_SECONDS", 180)
WEBHOOK_URL = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).strip().rstrip("/")
PORT = positive_int("PORT", 10000, 65535)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip() or hashlib.sha256(
    ("igbot-webhook:" + BOT_TOKEN).encode()
).hexdigest()

# Keep concurrency low because Instagram rate-limits aggressive automated requests.
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(positive_int("MAX_CONCURRENT_DOWNLOADS", 1, 4))

INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am", "www.instagr.am"}
INSTAGRAM_PATH_RE = re.compile(r"^/(?:(?:p|reel|reels|tv)/[A-Za-z0-9_-]+/?|stories/[A-Za-z0-9_.]+/[0-9]+/?)$", re.IGNORECASE)

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".mp4", ".mov", ".m4v", ".webm",
}

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ig-downloader-bot")
# HTTP request URLs include the Telegram bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        value = super().format(record)
        for secret in (BOT_TOKEN, IG_COOKIES_B64, WEBHOOK_SECRET):
            if secret:
                value = value.replace(secret, "[redacted]")
        return value


for handler in logging.getLogger().handlers:
    handler.setFormatter(RedactingFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))


def allowed_user_ids() -> set[int]:
    if not ALLOWED_USER_IDS_RAW:
        return set()
    ids: set[int] = set()
    for item in ALLOWED_USER_IDS_RAW.split(","):
        item = item.strip()
        if item:
            ids.add(int(item))
    return ids


ALLOWED_USER_IDS = allowed_user_ids()


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


def extract_instagram_url(text: str) -> str | None:
    # Accept a message that contains a URL, not only a bare URL.
    candidates = re.findall(r"https?://[^\s<>]+", text or "")
    for raw in candidates:
        url = raw.rstrip(".,!?;:)]}\"'")
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        if (host in INSTAGRAM_HOSTS and not parsed.username and not parsed.password
                and port in (None, 80, 443) and INSTAGRAM_PATH_RE.fullmatch(parsed.path or "")):
            # Remove tracking queries/fragments and force HTTPS to the canonical host.
            return urlunparse(("https", "www.instagram.com", parsed.path, "", "", ""))
    return None


def write_cookies_if_configured(workdir: Path) -> Path | None:
    if not IG_COOKIES_B64:
        return None
    cookie_path = workdir / "instagram-cookies.txt"
    try:
        cookie_bytes = base64.b64decode(IG_COOKIES_B64, validate=True)
    except Exception as exc:
        raise RuntimeError("IG_COOKIES_B64 is not valid base64") from exc
    cookie_path.write_bytes(cookie_bytes)
    cookie_path.chmod(0o600)
    return cookie_path


async def run_gallery_dl(url: str, output_dir: Path, cookie_path: Path | None) -> tuple[int, str]:
    cmd = [
        sys.executable, "-m", "gallery_dl", "--config-ignore", "--no-input", "--warning",
        "--directory", str(output_dir), "--range", f"1-{MAX_FILES}",
        "-o", "cache.file=:memory:", "-o", "extractor.instagram.user-cache=memory",
        "-o", 'downloader.ytdl.raw-options={"cachedir":false}',
    ]
    if cookie_path:
        cmd.extend(["--cookies", str(cookie_path)])
    cmd.append(url)

    logger.info("Starting Instagram download")
    child_env = os.environ.copy()
    for name in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "IG_COOKIES_B64", "WEBHOOK_SECRET"):
        child_env.pop(name, None)
    # Keep any incidental downloader cache/config inside the disposable workdir.
    child_env["XDG_CACHE_HOME"] = str(output_dir.parent / "cache")
    child_env["XDG_CONFIG_HOME"] = str(output_dir.parent / "config")
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=child_env, start_new_session=(os.name == "posix"),
    )
    reader = asyncio.create_task(proc.communicate())
    deadline = asyncio.get_running_loop().time() + DOWNLOAD_TIMEOUT_SECONDS
    try:
        while not reader.done():
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("Instagram download timed out")
            size = 0
            for path in output_dir.rglob("*"):
                try:
                    if path.is_file():
                        size += path.stat().st_size
                except FileNotFoundError:
                    pass  # ffmpeg/yt-dlp may rename a finished fragment during the scan.
            if size > MAX_TEMP_MB * 1024 * 1024:
                raise RuntimeError("Download exceeded the temporary storage limit")
            await asyncio.wait({reader}, timeout=0.25)
        stdout, _ = await reader
        return proc.returncode, stdout.decode("utf-8", errors="replace")
    finally:
        # Cancellation/timeout must stop yt-dlp and ffmpeg before the folder is deleted.
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif proc.returncode is None:
            proc.kill()
        await proc.wait()
        await reader


def collect_media_files(output_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in output_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith((".part", ".ytdl")):
            continue
        if path.suffix.lower() in MEDIA_EXTENSIONS:
            files.append(path)
    return sorted(files, key=lambda p: [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", p.name)])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert update.message
    await update.message.reply_text(
        "send me an instagram post, reel, carousel, or story link.\n\n"
        "i’ll fetch the best media available to the downloader and return it as uncompressed documents.\n\n"
        "created by rajnish sahani"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert update.message
    await update.message.reply_text(
        "paste an instagram link, for example:\n"
        "https://www.instagram.com/reel/...\n\n"
        "private/login-only content needs instagram cookies configured on the server."
    )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Reveals only the requesting user's own ID, even before the allowlist is set.
    if update.message and update.effective_user:
        await update.message.reply_text(f"your telegram user id: {update.effective_user.id}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    message = update.message
    if not message or not message.text:
        return

    url = extract_instagram_url(message.text)
    if not url:
        await message.reply_text("send me a valid instagram post/reel/story link.")
        return

    status = await message.reply_text("downloading the highest-quality media…")

    async with DOWNLOAD_SEMAPHORE:
        with tempfile.TemporaryDirectory(prefix="igbot-") as tmp:
            workdir = Path(tmp)
            output_dir = workdir / "media"
            output_dir.mkdir(parents=True, exist_ok=True)

            try:
                cookie_path = write_cookies_if_configured(workdir)
                returncode, output = await run_gallery_dl(url, output_dir, cookie_path)
                media_files = collect_media_files(output_dir)

                if not media_files:
                    logger.warning("gallery-dl returned %s without media", returncode)
                    lower = output.lower()
                    if "login" in lower or "cookies" in lower or "401" in lower:
                        text = (
                            "instagram blocked this request or requires login. "
                            "configure IG_COOKIES_B64 on the bot server, then try again."
                        )
                    elif "rate" in lower or "429" in lower:
                        text = "instagram is rate-limiting the bot. try again later."
                    else:
                        text = "i couldn’t download media from that link. it may be private, expired, or unavailable."
                    await status.edit_text(text)
                    return

                if returncode != 0:
                    await message.reply_text("some files could not be downloaded; sending the completed files.")
                if len(media_files) >= MAX_FILES:
                    await message.reply_text(f"this request is limited to {MAX_FILES} files.")
                await status.edit_text(f"found {len(media_files)} file(s). uploading original quality…")

                too_large: list[Path] = []
                sent = 0
                for index, path in enumerate(media_files, start=1):
                    size_mb = path.stat().st_size / (1024 * 1024)
                    if size_mb > MAX_UPLOAD_MB:
                        too_large.append(path)
                        continue

                    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
                    with path.open("rb") as fh:
                        await message.reply_document(
                            document=fh,
                            filename=path.name,
                            caption=(f"{index}/{len(media_files)} • original file" if len(media_files) > 1 else "original file"),
                            read_timeout=120,
                            write_timeout=120,
                            connect_timeout=30,
                            pool_timeout=30,
                        )
                    sent += 1

                if too_large:
                    names = ", ".join(f"{p.name} ({p.stat().st_size / (1024*1024):.1f} MB)" for p in too_large[:3])
                    extra = "" if len(too_large) <= 3 else f" and {len(too_large)-3} more"
                    await message.reply_text(
                        f"{len(too_large)} file(s) are above this bot’s {MAX_UPLOAD_MB} MB upload limit: {names}{extra}. "
                        "this bot sends files without reducing quality; oversized files are skipped."
                    )

                if sent:
                    await status.delete()
                else:
                    await status.edit_text("the media was downloaded, but every file exceeded the configured telegram upload limit.")

            except Exception as exc:
                logger.exception("Unhandled download error")
                await status.edit_text(f"download failed: {type(exc).__name__}. check the bot logs for details.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Telegram update error", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is required")

    if os.environ.get("RENDER") == "true" and not WEBHOOK_URL:
        raise SystemExit("Render requires RENDER_EXTERNAL_URL or WEBHOOK_URL")
    if WEBHOOK_URL:
        parsed = urlparse(WEBHOOK_URL)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
            raise SystemExit("WEBHOOK_URL must be an HTTPS origin, for example https://name.onrender.com")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
            raise SystemExit("WEBHOOK_SECRET must contain 1-256 letters, digits, underscores or hyphens")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    if WEBHOOK_URL:
        logger.info("Bot starting in webhook mode on port %s", PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="telegram",
            webhook_url=f"{WEBHOOK_URL}/telegram",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message"],
            drop_pending_updates=False,
        )
    else:
        logger.info("Bot starting in polling mode")
        app.run_polling(allowed_updates=["message"], drop_pending_updates=False)


if __name__ == "__main__":
    main()
