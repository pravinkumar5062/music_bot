import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
import os
import tempfile

try:
    import fcntl
except ImportError:  # Windows/local development
    fcntl = None
from dataclasses import dataclass
from typing import Dict, List, Optional

import yt_dlp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from pyrogram import Client as PyrogramClient
import pyrogram.errors
if not hasattr(pyrogram.errors, "GroupcallForbidden"):
    pyrogram.errors.GroupcallForbidden = type("GroupcallForbidden", (Exception,), {})

from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream

try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
except Exception:  # pragma: no cover - fallback for older yt-dlp builds
    ImpersonateTarget = None

load_dotenv()

TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
    or os.getenv("TOKEN")
)
API_ID = int(os.getenv("TELEGRAM_API_ID") or os.getenv("API_ID") or 0)
API_HASH = os.getenv("TELEGRAM_API_HASH") or os.getenv("API_HASH") or ""
SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME") or "music_bot_group"

GROUP_CALL_CLIENT: Optional[object] = None
GROUP_CALL_INSTANCE: Optional[object] = None


@dataclass
class Song:
    title: str
    url: str
    duration: int
    thumbnail: str
    uploader: str
    file_path: str = ""
    headers: dict = None


CHAT_STATES: Dict[int, Dict[str, object]] = {}


def get_state(chat_id: int) -> Dict[str, object]:
    if chat_id not in CHAT_STATES:
        CHAT_STATES[chat_id] = {
            "queue": [],
            "current": None,
            "playing": False,
        }
    return CHAT_STATES[chat_id]


def build_ydl_opts(*, download: bool = False) -> dict:
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": not download,
        "socket_timeout": 60,
        "extractor_retries": 5,
        "extractor_args": {"youtube": {"player_client": ["tv", "android", "web"]}},
        "js_runtimes": {"node": {}},
    }
    
    if ImpersonateTarget is not None:
        opts["impersonate"] = ImpersonateTarget(client="chrome")


    # Support cookies from browser (e.g. YOUTUBE_COOKIES_FROM_BROWSER=chrome)
    browser_cookies = os.getenv("YOUTUBE_COOKIES_FROM_BROWSER")
    if browser_cookies:
        if ":" in browser_cookies:
            browser, profile = browser_cookies.split(":", 1)
            opts["cookiesfrombrowser"] = (browser, profile)
        else:
            opts["cookiesfrombrowser"] = (browser_cookies,)
    else:
        # Support cookies file for bypassing bot protection
        cookie_path = os.getenv("YOUTUBE_COOKIES_FILE", "cookies.txt")
        if os.path.exists(cookie_path):
            opts["cookiefile"] = cookie_path
        elif "YOUTUBE_COOKIES" in os.environ:
            cookie_text = os.environ["YOUTUBE_COOKIES"]
            if not hasattr(build_ydl_opts, "temp_cookie_file"):
                fd, path = tempfile.mkstemp(prefix="yt_cookies_", suffix=".txt", text=True)
                with os.fdopen(fd, 'w') as f:
                    if not cookie_text.startswith("# Netscape HTTP Cookie File"):
                        f.write("# Netscape HTTP Cookie File\n")
                    f.write(cookie_text)
                build_ydl_opts.temp_cookie_file = path
            opts["cookiefile"] = build_ydl_opts.temp_cookie_file

    return opts


async def search_song(query: str) -> Song:
    last_error = None

    for search_prefix in ("ytsearch1:", "scsearch1:"):
        ydl_opts = build_ydl_opts(download=False)
        ydl_opts.update({"extract_flat": False})

        loop = asyncio.get_running_loop()

        def _extract() -> dict:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(f"{search_prefix}{query}", download=False)

        try:
            info = await loop.run_in_executor(None, _extract)
            if isinstance(info, dict) and "entries" in info and info["entries"]:
                entry = info["entries"][0]
            else:
                entry = info

            if not isinstance(entry, dict) or not entry:
                raise ValueError("No result found for your query.")

            url = entry.get("url") or entry.get("webpage_url")
            if not url or str(url).startswith(("ytsearch:", "ytsearch1:", "scsearch:", "scsearch1:")):
                raise ValueError("Search returned an unusable URL.")

            return Song(
                title=entry.get("title", "Untitled song"),
                url=url,
                duration=int(entry.get("duration", 0) or 0),
                thumbnail=entry.get("thumbnail", ""),
                uploader=entry.get("uploader", "Unknown artist"),
            )
        except Exception as exc:
            last_error = exc

    raise RuntimeError(
        "YouTube and SoundCloud search both failed. Try a different song title or a direct link. "
        f"Last error: {last_error}"
    )


async def get_stream_url(song: Song) -> Song:
    ydl_opts = build_ydl_opts(download=False)
    ydl_opts.update({
        "extract_flat": False,
    })

    loop = asyncio.get_running_loop()

    def _extract() -> dict:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(song.url, download=False)

    info = await loop.run_in_executor(None, _extract)
    
    if not info.get("url"):
        raise ValueError("Could not extract a valid stream URL.")
        
    song.file_path = info["url"]
    song.headers = info.get("http_headers")
    return song


async def _start_group_call(chat_id: int) -> Optional[object]:
    global GROUP_CALL_CLIENT, GROUP_CALL_INSTANCE

    if PyrogramClient is None or PyTgCalls is None or MediaStream is None or not API_ID or not API_HASH:
        return None

    if GROUP_CALL_CLIENT is None:
        session_string = os.getenv("TELEGRAM_SESSION_STRING")
        if not session_string:
            raise RuntimeError("TELEGRAM_SESSION_STRING environment variable is missing! You must generate a Pyrogram session string locally and add it to Render so the bot can join voice chats.")
            
        GROUP_CALL_CLIENT = PyrogramClient(
            SESSION_NAME,
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_string,
            workdir=tempfile.gettempdir(),
        )
        GROUP_CALL_INSTANCE = PyTgCalls(GROUP_CALL_CLIENT)
        await GROUP_CALL_INSTANCE.start()

    return GROUP_CALL_INSTANCE


async def _play_in_group(chat_id: int, song: Song) -> bool:
    group_call = await _start_group_call(chat_id)
    if group_call is None:
        raise RuntimeError("Group call client failed to initialize. Are TELEGRAM_API_ID and TELEGRAM_API_HASH set correctly?")

    try:
        # Resolve the chat peer to prevent 'Peer id invalid' errors on fresh sessions
        try:
            await GROUP_CALL_CLIENT.get_chat(chat_id)
        except Exception as e:
            # If get_chat fails (usually because the integer ID is not cached), fetch dialogs to cache peers
            try:
                async for _ in GROUP_CALL_CLIENT.get_dialogs(limit=50):
                    pass
                await GROUP_CALL_CLIENT.get_chat(chat_id)
            except Exception as e2:
                raise RuntimeError(f"Failed to resolve chat ID {chat_id} for the Assistant account. Ensure the Assistant account is actually a member of this group! Error: {e2}")

        await group_call.play(
            chat_id,
            MediaStream(
                song.file_path, 
                video_flags=MediaStream.Flags.IGNORE,
                headers=song.headers
            ),
        )
        return True
    except Exception as e:
        raise RuntimeError(f"PyTgCalls error: {e}")


async def play_next(chat_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(chat_id)
    queue: List[Song] = state["queue"]

    if not queue:
        state["playing"] = False
        state["current"] = None
        await update.effective_message.reply_text("Queue is empty. Add songs with /play.")
        return

    current = queue.pop(0)
    state["current"] = current
    state["playing"] = True

    try:
        stream_data = await get_stream_url(current)

        if await _play_in_group(chat_id, stream_data):
            await update.effective_message.reply_text(
                f"🎧 Streaming in voice chat: {stream_data.title}"
            )
            
            # Delete the previous song's file to save disk space on Render
            last_file = state.get("last_file_path")
            if last_file and os.path.exists(last_file):
                try:
                    os.remove(last_file)
                except Exception:
                    pass
            state["last_file_path"] = stream_data.file_path
            
        else:
            await update.effective_message.reply_text(
                "Group call is not configured correctly or bot is not an admin."
            )
    except Exception as exc:
        await update.effective_message.reply_text(f"Playback failed: {exc}")
        state["playing"] = False
        return

    if queue:
        await update.effective_message.reply_text(f"Queued {len(queue)} more song(s).")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎵 Advanced Music Bot is ready.\n"
        "Use /play <song name> to add a track.\n"
        "Use /queue to view the queue.\n"
        "Use /skip to jump to the next song.\n"
        "Use /now to see what is playing.\n"
        "Use /stop to clear the queue."
    )


async def play(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /play <song name or YouTube URL>")
        return

    try:
        song = await search_song(query)
        state = get_state(update.effective_chat.id)
        state["queue"].append(song)
        await update.message.reply_text(
            f"✅ Added to queue: {song.title}\n"
            f"Artist: {song.uploader}\n"
            f"Duration: {song.duration}s"
        )

        if not state["playing"]:
            await play_next(update.effective_chat.id, update, context)
    except Exception as exc:
        message = str(exc)
        if "sign in to confirm" in message.lower() or "drm protected" in message.lower() or "are you a bot" in message.lower():
            message = (
                "This song is blocked by YouTube in this environment. "
                "Please try another track or a direct link from a different source."
            )
        await update.message.reply_text(f"Unable to load song: {message}")


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    current = state.get("current")
    songs = state.get("queue", [])
    lines = ["🎶 Current queue:"]

    if current:
        lines.append(f"Now playing: {current.title}")
    else:
        lines.append("Now playing: nothing")

    if songs:
        for index, song in enumerate(songs, start=1):
            lines.append(f"{index}. {song.title} — {song.uploader}")
    else:
        lines.append("No songs queued.")

    await update.message.reply_text("\n".join(lines))


async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    if not state["queue"] and not state["current"]:
        await update.message.reply_text("Nothing to skip.")
        return

    await update.message.reply_text("⏭ Skipping current song...")
    await play_next(update.effective_chat.id, update, context)


async def now_playing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    current = state.get("current")
    if current:
        await update.message.reply_text(
            f"🎵 Now playing: {current.title}\nArtist: {current.uploader}\nDuration: {current.duration}s"
        )
    else:
        await update.message.reply_text("Nothing is playing right now.")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    state["queue"] = []
    state["current"] = None
    state["playing"] = False
    await update.message.reply_text("🛑 Playback stopped and queue cleared.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Available commands:\n"
        "/play <song> - search and queue a song\n"
        "/queue - show the current queue\n"
        "/skip - play the next song\n"
        "/now - show current song\n"
        "/stop - clear the queue"
    )


def main() -> None:
    if not TOKEN:
        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN (or BOT_TOKEN/TOKEN) in your environment or .env file."
        )

    port = int(os.getenv("PORT", "10000"))
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url and os.getenv("RENDER_EXTERNAL_URL"):
        webhook_url = f"{os.getenv('RENDER_EXTERNAL_URL').rstrip('/')}/telegram"

    lock_fd = None
    if fcntl is not None:
        try:
            lock_path = os.path.join(tempfile.gettempdir(), "music_bot_single_instance.lock")
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            if lock_fd is not None:
                os.close(lock_fd)
            raise RuntimeError("Another bot instance is already running on this container.") from exc

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("play", play))
    application.add_handler(CommandHandler("queue", queue_cmd))
    application.add_handler(CommandHandler("skip", skip))
    application.add_handler(CommandHandler("now", now_playing))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("help", help_cmd))

    try:
        if webhook_url:
            application.run_webhook(
                listen="0.0.0.0",
                port=port,
                url_path="telegram",
                webhook_url=webhook_url,
                drop_pending_updates=True,
            )
        else:
            application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    finally:
        if lock_fd is not None and fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


if __name__ == "__main__":
    main()
