import asyncio
import os
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional

import yt_dlp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

try:
    from pyrogram import Client as PyrogramClient
    from pytgcalls import PyTgCalls
    from pytgcalls.types import MediaStream
except Exception:  # pragma: no cover - optional group-call dependencies
    PyrogramClient = None
    PyTgCalls = None
    MediaStream = None

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
        "retries": 5,
        "extractor_retries": 5,
    }

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


async def download_song(song: Song) -> Song:
    temp_dir = tempfile.gettempdir()
    outtmpl = os.path.join(temp_dir, f"music_{abs(hash(song.url))}.%(ext)s")

    ydl_opts = build_ydl_opts(download=True)
    ydl_opts.update({
        "outtmpl": outtmpl,
        "postprocessors": [],
        "geo_bypass": True,
        "nocheckcertificate": True,
        "prefer_ffmpeg": True,
    })

    loop = asyncio.get_running_loop()

    def _download() -> None:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([song.url])

    await loop.run_in_executor(None, _download)

    candidate = None
    audio_exts = (".mp3", ".m4a", ".webm", ".ogg", ".opus", ".wav")
    for file_name in sorted(os.listdir(temp_dir), key=lambda name: os.path.getmtime(os.path.join(temp_dir, name)), reverse=True):
        if file_name.startswith("music_") and file_name.lower().endswith(audio_exts):
            candidate = os.path.join(temp_dir, file_name)
            break

    if candidate is None:
        raise FileNotFoundError("Audio file could not be downloaded.")

    song.file_path = candidate
    return song


async def _start_group_call(chat_id: int) -> Optional[object]:
    global GROUP_CALL_CLIENT, GROUP_CALL_INSTANCE

    if PyrogramClient is None or PyTgCalls is None or MediaStream is None or not API_ID or not API_HASH:
        return None

    if GROUP_CALL_CLIENT is None:
        GROUP_CALL_CLIENT = PyrogramClient(
            SESSION_NAME,
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=tempfile.gettempdir(),
        )
        GROUP_CALL_INSTANCE = PyTgCalls(GROUP_CALL_CLIENT)
        await GROUP_CALL_INSTANCE.start()

    return GROUP_CALL_INSTANCE


async def _play_in_group(chat_id: int, file_path: str) -> bool:
    group_call = await _start_group_call(chat_id)
    if group_call is None:
        return False

    try:
        await group_call.play(
            chat_id,
            MediaStream(file_path, video_flags=MediaStream.Flags.IGNORE),
        )
        return True
    except Exception:
        return False


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
        downloaded = await download_song(current)

        if await _play_in_group(chat_id, downloaded.file_path):
            await update.effective_message.reply_text(
                f"🎧 Playing in voice chat: {downloaded.title}"
            )
        else:
            await update.effective_message.reply_audio(
                audio=downloaded.file_path,
                title=downloaded.title,
                performer=downloaded.uploader,
                duration=downloaded.duration,
                caption=f"Now playing: {downloaded.title}",
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
        if "Sign in to confirm" in message or "DRM protected" in message or "bot" in message.lower():
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

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("play", play))
    application.add_handler(CommandHandler("queue", queue_cmd))
    application.add_handler(CommandHandler("skip", skip))
    application.add_handler(CommandHandler("now", now_playing))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("help", help_cmd))

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
