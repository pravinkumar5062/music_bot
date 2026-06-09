import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
import os
import tempfile
import logging
from collections import deque

# Custom logger to store the last 50 logs in memory so we can view them on Render
log_history = deque(maxlen=50)

class MemoryHandler(logging.Handler):
    def emit(self, record):
        log_history.append(self.format(record))

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Enable DEBUG logging for PyTgCalls to catch WebRTC/FFmpeg errors
logging.getLogger("pytgcalls").setLevel(logging.DEBUG)

memory_handler = MemoryHandler()
memory_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
logging.getLogger().addHandler(memory_handler)
logging.getLogger("pytgcalls").addHandler(memory_handler)

try:
    import imageio_ffmpeg
    # Prepend to PATH so it overrides any broken system FFmpeg
    os.environ["PATH"] = os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe()) + os.pathsep + os.environ["PATH"]
except ImportError:
    pass

try:
    import fcntl
except ImportError:  # Windows/local development
    fcntl = None
from dataclasses import dataclass
from typing import Dict, List, Optional

import yt_dlp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

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
    requested_by: str = "Unknown"


CHAT_STATES: Dict[int, Dict[str, object]] = {}




def escape_md(text: str) -> str:
    if not text:
        return ""
    escape_chars = r"_*[]()~`>#+-=|{}.!"
    import re
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", str(text))


def get_start_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = [
        [InlineKeyboardButton("✨ 🔍 Play Music", callback_data="cmd_play")],
        [
            InlineKeyboardButton("✨ 📜 View Queue", callback_data="cmd_queue"),
            InlineKeyboardButton("✨ 🎧 Now Playing", callback_data="cmd_now")
        ],
        [
            InlineKeyboardButton("✨ ⏭ Skip Track", callback_data="cmd_skip"),
            InlineKeyboardButton("✨ 🛑 Stop Music", callback_data="cmd_stop")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_player_keyboard(state: Dict[str, object] = None) -> InlineKeyboardMarkup:
    if not state: state = {}
    is_paused = state.get("paused", False)
    is_shuffle = state.get("shuffle", False)
    repeat_mode = state.get("repeat", 0)
    
    play_pause_btn = InlineKeyboardButton("▶️ Resume", callback_data="btn_resume") if is_paused else InlineKeyboardButton("⏸ Pause", callback_data="btn_pause")
    shuffle_text = "🔀 Shuffle On" if is_shuffle else "🔀 Shuffle"
    repeat_text = "🔁 Repeat On" if repeat_mode == 1 else "🔂 Repeat One" if repeat_mode == 2 else "🔁 Repeat"
    
    keyboard = [
        [
            InlineKeyboardButton("⏮ Previous", callback_data="btn_prev"),
            play_pause_btn,
            InlineKeyboardButton("⏭ Next", callback_data="btn_skip")
        ],
        [
            InlineKeyboardButton(shuffle_text, callback_data="btn_shuffle"),
            InlineKeyboardButton(repeat_text, callback_data="btn_repeat")
        ],
        [
            InlineKeyboardButton("🔉 Volume", callback_data="btn_volume"),
            InlineKeyboardButton("✨ Effects", callback_data="btn_effects")
        ],
        [
            InlineKeyboardButton("❌ Close Player", callback_data="btn_close")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_state(chat_id: int) -> Dict[str, object]:
    if chat_id not in CHAT_STATES:
        CHAT_STATES[chat_id] = {
            "queue": [],
            "history": [],
            "current": None,
            "playing": False,
            "paused": False,
            "shuffle": False,
            "repeat": 0,
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
        ydl_opts.update({"extract_flat": True})

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

    def _extract() -> str:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(song.url, download=False)
            return info.get("url", song.url)

    song.file_path = await loop.run_in_executor(None, _extract)
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
                video_flags=MediaStream.Flags.IGNORE
            ),
        )
        
        # Explicitly force unmute after 3 seconds (some groups mute new participants by default)
        # We must wait for WebRTC to connect and MTProto to cache the call object before unmuting!
        async def delayed_unmute():
            await asyncio.sleep(3)
            try:
                await group_call.unmute(chat_id)
                logging.info(f"[WebRTC] 🎤 Explicitly sent UNMUTE request for {chat_id}")
            except Exception as e:
                logging.error(f"[WebRTC] ⚠️ Failed to unmute: {e}")
                
        asyncio.create_task(delayed_unmute())
            
        return True
    except Exception as e:
        raise RuntimeError(f"PyTgCalls error: {e}")


async def play_next(chat_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE, is_previous: bool = False) -> None:
    state = get_state(chat_id)
    queue: List[Song] = state["queue"]
    history: List[Song] = state.setdefault("history", [])
    
    current_song = state.get("current")

    if is_previous:
        if not history:
            await update.effective_message.reply_text("No previous songs in history.")
            return
        if current_song:
            queue.insert(0, current_song)
        next_song = history.pop()
    else:
        if current_song:
            history.append(current_song)
            if len(history) > 50:
                history.pop(0)
            
            repeat = state.get("repeat", 0)
            if repeat == 1:
                queue.append(current_song)
            elif repeat == 2:
                queue.insert(0, current_song)
                
        # Autoplay if queue is STILL empty (e.g. not repeating)
        if not queue and current_song:
            status_msg = await update.effective_message.reply_text("✨ *Autoplay:* Fetching next recommended song...", parse_mode="Markdown")
            try:
                import re
                from ytmusicapi import YTMusic
                match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", current_song.url)
                if match:
                    video_id = match.group(1)
                    ytm = YTMusic()
                    # Run in executor to prevent blocking the async loop
                    watch_playlist = await asyncio.to_thread(ytm.get_watch_playlist, videoId=video_id)
                    tracks = watch_playlist.get("tracks", [])
                    
                    next_track = None
                    for t in tracks:
                        if t.get("videoId") != video_id:
                            next_track = t
                            break
                            
                    if next_track:
                        thumb = next_track.get("thumbnail")
                        thumb_url = thumb[0]["url"] if isinstance(thumb, list) and len(thumb) > 0 else ""
                        
                        duration = 0
                        length_str = next_track.get("length")
                        if length_str and ":" in length_str:
                            parts = length_str.split(":")
                            if len(parts) == 2:
                                duration = int(parts[0]) * 60 + int(parts[1])
                            elif len(parts) == 3:
                                duration = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                                
                        auto_song = Song(
                            title=next_track.get("title", "Untitled"),
                            url=f"https://www.youtube.com/watch?v={next_track.get('videoId')}",
                            duration=duration,
                            thumbnail=thumb_url,
                            uploader=", ".join([a.get("name", "") for a in next_track.get("artists", [])]),
                            requested_by="🤖 Autoplay"
                        )
                        queue.append(auto_song)
            except Exception as e:
                logging.error(f"Autoplay failed: {e}")
            await status_msg.delete()

        if not queue:
            state["playing"] = False
            state["current"] = None
            if GROUP_CALL_INSTANCE:
                try:
                    await GROUP_CALL_INSTANCE.leave_call(chat_id)
                except Exception:
                    pass
            await update.effective_message.reply_text("Queue is empty. Playback stopped. Add songs with /play.")
            return

        if state.get("shuffle", False) and len(queue) > 1:
            import random
            idx = random.randint(0, len(queue) - 1)
            next_song = queue.pop(idx)
        else:
            next_song = queue.pop(0)

    state["current"] = next_song
    state["playing"] = True
    current = next_song

    try:
        stream_data = await get_stream_url(current)

        if not stream_data.file_path:
            raise RuntimeError("Failed to extract streaming URL.")

        if await _play_in_group(chat_id, stream_data):
            msg_text = (
                f"🎵 *Music Playlist:*\n\n"
                f"1. 🎸 {escape_md(stream_data.title)} — {escape_md(stream_data.uploader)}\n"
                f"🏆 *Requested by:* {escape_md(stream_data.requested_by)}\n\n"
                f"⏱ {stream_data.duration // 60}:{stream_data.duration % 60:02d} ▷ ───────────"
            )
            state["player_message"] = await update.effective_message.reply_text(
                msg_text,
                parse_mode="Markdown",
                reply_markup=get_player_keyboard(state)
            )
            
            # No local files to delete since we are directly streaming!
            
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
    state = get_state(update.effective_chat.id)
    await update.message.reply_text(
        "🎵 *Advanced Music Bot*\n"
        "━━━━━━━━━━━━━━━\n"
        "Welcome! I am ready to play your favorite tracks directly in the Voice Chat. 🎧\n\n"
        "👇 *Tap the colorful buttons below to control the music!*\n"
        "• *Play Music:* Opens the typing bar to search for a song.\n"
        "• *Other Buttons:* Instantly execute their actions with one tap!",
        parse_mode="Markdown",
        reply_markup=get_start_keyboard()
    )


async def play(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /play <song name or YouTube URL>")
        return

    try:
        song = await search_song(query)
        song.requested_by = update.effective_user.first_name if update.effective_user else "Unknown"
        state = get_state(update.effective_chat.id)
        state["queue"].append(song)
        await update.message.reply_text(
            f"✅ *Added to Queue*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🎵 *Track:* {escape_md(song.title)}\n"
            f"👤 *Artist:* {escape_md(song.uploader)}\n"
            f"⏱ *Duration:* {song.duration // 60}m {song.duration % 60}s",
            parse_mode="Markdown"
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
    
    lines = ["📜 *Current Queue*\n━━━━━━━━━━━━━━━"]

    if current:
        lines.append(f"🔊 *Playing Now:*\n{escape_md(current.title)}\n")
    else:
        lines.append("🔊 *Playing Now:*\n_Nothing_\n")

    if songs:
        lines.append("⏳ *Up Next:*")
        for index, song in enumerate(songs, start=1):
            lines.append(f"{index}️⃣ {escape_md(song.title)} — {escape_md(song.uploader)}")
    else:
        lines.append("📭 _No songs in queue._")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
        msg_text = (
            f"🎵 *Music Playlist:*\n\n"
            f"1. 🎸 {escape_md(current.title)} — {escape_md(current.uploader)}\n"
            f"🏆 *Requested by:* {escape_md(current.requested_by)}\n\n"
            f"⏱ {current.duration // 60}:{current.duration % 60:02d} ▷ ───────────"
        )
        await update.message.reply_text(
            msg_text,
            parse_mode="Markdown",
            reply_markup=get_player_keyboard(state)
        )
    else:
        await update.message.reply_text("🔇 _Nothing is playing right now._", parse_mode="Markdown")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    state["queue"] = []
    state["current"] = None
    state["playing"] = False
    if GROUP_CALL_INSTANCE:
        try:
            await GROUP_CALL_INSTANCE.leave_call(chat_id)
        except Exception:
            pass
    await update.message.reply_text("🛑 Playback stopped and queue cleared.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    await update.message.reply_text(
        "🎵 *Advanced Music Bot*\n"
        "━━━━━━━━━━━━━━━\n"
        "*Commands:*\n"
        "🔍 `/play <song>` • Search & add to queue\n"
        "📜 `/queue` • View the upcoming tracks\n"
        "⏭ `/skip` • Jump to the next song\n"
        "🎼 `/now` • See the current track\n"
        "⏹ `/stop` • Clear queue & disconnect",
        parse_mode="Markdown",
        reply_markup=get_start_keyboard()
    )


async def diagnostics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logs = "\n".join(log_history)
    if not logs:
        logs = "No logs recorded yet."
    
    # Send logs in chunks if too long
    if len(logs) > 4000:
        logs = logs[-4000:]
        
    await update.message.reply_text(f"📝 **System Logs:**\n```\n{logs}\n```", parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    
    data = query.data
    chat_id = query.message.chat.id
    state = get_state(chat_id)
    

    if data == "cmd_play":
        from telegram import ForceReply
        await query.message.reply_text(
            "🎵 *What do you want to play?*\n\n✨ _Type the name of the song or send a YouTube link below:_",
            parse_mode="Markdown",
            reply_markup=ForceReply(selective=True)
        )
        return
    elif data == "cmd_queue":
        await query.answer()
        await queue_cmd(update, context)
        return
    elif data == "cmd_now":
        await query.answer()
        await now_playing(update, context)
        return
    elif data == "cmd_skip":
        await query.answer()
        await skip(update, context)
        return
    elif data == "cmd_stop":
        await query.answer()
        await stop(update, context)
        return

    if data in ["btn_not_impl", "btn_volume", "btn_effects"]:
        await query.answer("This feature is not supported in high-speed streaming mode!", show_alert=True)
        return
        
    if data == "btn_prev":
        if not state.get("history"):
            await query.answer("No previous song!", show_alert=True)
            return
        await query.answer("Playing previous song...")
        await play_next(chat_id, update, context, is_previous=True)
        return
        
    if data == "btn_shuffle":
        state["shuffle"] = not state.get("shuffle", False)
        await query.answer(f"Shuffle {'enabled' if state['shuffle'] else 'disabled'}")
        if state.get("current"):
            await query.edit_message_reply_markup(reply_markup=get_player_keyboard(state))
        return
        
    if data == "btn_repeat":
        state["repeat"] = (state.get("repeat", 0) + 1) % 3
        modes = ["disabled", "Repeat All", "Repeat One"]
        await query.answer(f"Repeat mode: {modes[state['repeat']]}")
        if state.get("current"):
            await query.edit_message_reply_markup(reply_markup=get_player_keyboard(state))
        return
        
    if data == "btn_skip":
        await query.answer("Skipping...")
        await play_next(chat_id, update, context)
        return
        
    if data == "btn_close":
        await query.answer("Playback closed.")
        state["queue"] = []
        state["current"] = None
        state["playing"] = False
        await query.message.delete()
        if GROUP_CALL_INSTANCE:
            try:
                await GROUP_CALL_INSTANCE.leave_call(chat_id)
            except Exception:
                pass
        return
        
    if data == "btn_pause" or data == "btn_resume":
        if not GROUP_CALL_INSTANCE:
            await query.answer("Not playing", show_alert=True)
            return
        try:
            if data == "btn_pause":
                await GROUP_CALL_INSTANCE.pause_stream(chat_id)
                state["paused"] = True
                await query.answer("Paused")
            else:
                await GROUP_CALL_INSTANCE.resume_stream(chat_id)
                state["paused"] = False
                await query.answer("Resumed")
                
            current = state.get("current")
            if current:
                msg_text = (
                    f"🎵 *Music Playlist:*\n\n"
                    f"1. 🎸 {escape_md(current.title)} — {escape_md(current.uploader)}\n"
                    f"🏆 *Requested by:* {escape_md(current.requested_by)}\n\n"
                    f"⏱ {current.duration // 60}:{current.duration % 60:02d} ▷ ───────────"
                )
                await query.edit_message_text(
                    text=msg_text,
                    parse_mode="Markdown",
                    reply_markup=get_player_keyboard(state)
                )
        except Exception as e:
            logging.error(f"Failed to pause/resume: {e}")
            await query.answer("Error occurred", show_alert=True)


async def handle_force_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.reply_to_message:
        if "What do you want to play?" in update.message.reply_to_message.text:
            context.args = update.message.text.split()
            await play(update, context)

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
    application.add_handler(CommandHandler("diagnostics", diagnostics_cmd))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.REPLY & filters.TEXT, handle_force_reply))

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
