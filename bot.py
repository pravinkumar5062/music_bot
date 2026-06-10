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
BOT_INSTANCE = None


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

def get_player_keyboard(state: Dict[str, object] = None, duration_str: str = "0:00 ▷ ─────────── 0:00") -> InlineKeyboardMarkup:
    if not state: state = {}
    is_paused = state.get("paused", False)
    is_shuffle = state.get("shuffle", False)
    repeat_mode = state.get("repeat", 0)
    
    play_pause_btn = InlineKeyboardButton("▶️ Resume", callback_data="btn_resume", style="success") if is_paused else InlineKeyboardButton("⏸ Pause", callback_data="btn_pause", style="success")
    shuffle_text = "🔀 Shuffle On" if is_shuffle else "🔀 Shuffle"
    repeat_text = "🔁 Repeat On" if repeat_mode == 1 else "🔂 Repeat One" if repeat_mode == 2 else "🔁 Repeat"
    
    keyboard = [
        [
            InlineKeyboardButton(duration_str, callback_data="ignore")
        ],
        [
            InlineKeyboardButton("⏮ Previous", callback_data="btn_prev"),
            play_pause_btn,
            InlineKeyboardButton("⏭ Next", callback_data="btn_skip")
        ],
        [
            InlineKeyboardButton(shuffle_text, callback_data="btn_shuffle"),
            InlineKeyboardButton("⏩ Seek", callback_data="ignore"),
            InlineKeyboardButton("🔁 Replay", callback_data="ignore")
        ],
        [
            InlineKeyboardButton("🔉 Volume", callback_data="btn_volume"),
            InlineKeyboardButton(repeat_text, callback_data="btn_repeat"),
            InlineKeyboardButton("✨ Effects", callback_data="btn_effects")
        ],
        [
            InlineKeyboardButton("❌ Close", callback_data="btn_close", style="danger")
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
        "extractor_retries": 1,
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
    # If direct URL, fallback to yt-dlp to extract metadata
    if query.startswith("http://") or query.startswith("https://"):
        ydl_opts = build_ydl_opts(download=False)
        ydl_opts.update({"extract_flat": True})
        loop = asyncio.get_running_loop()
        def _extract() -> dict:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(query, download=False)
        info = await loop.run_in_executor(None, _extract)
        if isinstance(info, dict) and "entries" in info and info["entries"]:
            entry = info["entries"][0]
        else:
            entry = info
            
        url = entry.get("url") or entry.get("webpage_url") or query
        return Song(
            title=entry.get("title", "Untitled song"),
            url=url,
            duration=int(entry.get("duration", 0) or 0),
            thumbnail=entry.get("thumbnail", ""),
            uploader=entry.get("uploader", "Unknown artist"),
        )
        
    # Extremely fast search via ytmusicapi JSON API
    try:
        from ytmusicapi import YTMusic
        ytm = YTMusic()
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, lambda: ytm.search(query, filter="songs", limit=1))
        if not results:
            results = await loop.run_in_executor(None, lambda: ytm.search(query, limit=1))
            
        if results:
            entry = results[0]
            video_id = entry.get("videoId")
            if video_id:
                thumb = entry.get("thumbnails", [{"url": ""}])
                thumb_url = thumb[-1]["url"] if isinstance(thumb, list) and len(thumb) > 0 else ""
                
                duration = 0
                length_str = entry.get("duration")
                if length_str and ":" in length_str:
                    parts = length_str.split(":")
                    if len(parts) == 2:
                        duration = int(parts[0]) * 60 + int(parts[1])
                    elif len(parts) == 3:
                        duration = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])

                return Song(
                    title=entry.get("title", "Untitled song"),
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    duration=duration,
                    thumbnail=thumb_url,
                    uploader=", ".join([a.get("name", "") for a in entry.get("artists", [])]),
                )
    except Exception as e:
        logging.error(f"ytmusicapi search failed: {e}")
        
    raise RuntimeError("Failed to find any matching songs.")


async def get_stream_url(song: Song) -> Song:
    # First, attempt to use ultra-fast pytubefix for YouTube URLs
    if "youtube.com" in song.url or "youtu.be" in song.url:
        try:
            loop = asyncio.get_running_loop()
            def _extract_pytube():
                from pytubefix import YouTube
                yt = YouTube(song.url, use_oauth=False, allow_oauth_cache=False, use_po_token=False)
                audio_streams = yt.streams.filter(only_audio=True)
                audio = audio_streams.order_by('abr').desc().first()
                if audio and audio.url:
                    return audio.url
                return None
            
            stream_url = await loop.run_in_executor(None, _extract_pytube)
            if stream_url:
                song.file_path = stream_url
                return song
        except Exception as e:
            logging.error(f"Pytubefix failed, falling back to yt-dlp: {e}")

    # Fallback to yt-dlp
    ydl_opts = build_ydl_opts(download=False)
    ydl_opts.update({
        "extract_flat": False,
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
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
        
        @GROUP_CALL_INSTANCE.on_update()
        async def stream_update_handler(client, update):
            from pytgcalls.types import StreamEnded
            if isinstance(update, StreamEnded):
                # When a song naturally finishes, automatically trigger play_next
                chat_id = update.chat_id
                asyncio.create_task(play_next(chat_id, None, None))
                
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


async def play_next(chat_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE, is_previous: bool = False, play_messages=None) -> None:
    state = get_state(chat_id)
    queue: List[Song] = state["queue"]
    history: List[Song] = state.setdefault("history", [])
    
    current_song = state.get("current")

    if is_previous:
        if not history:
            bot = context.bot if context else BOT_INSTANCE
            if bot:
                try:
                    await bot.send_message(chat_id=chat_id, text="No previous songs in history.")
                except Exception: pass
            return
        if current_song:
            queue.insert(0, current_song)
        next_song = history.pop()
        
        # Send previous message
        bot = context.bot if context else BOT_INSTANCE
        if bot:
            try:
                prev_msg = await bot.send_message(
                    chat_id=chat_id,
                    text="⏮ *Switching to previous song...*",
                    parse_mode="Markdown"
                )
                if play_messages is None:
                    play_messages = []
                play_messages.append(prev_msg)
            except Exception: pass
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
                
            # If it's a manual skip (update is not None) and there are songs in the queue, notify the user
            if update is not None and queue:
                bot = context.bot if context else BOT_INSTANCE
                if bot:
                    try:
                        next_msg = await bot.send_message(
                            chat_id=chat_id,
                            text="⏭ *Switching to next song...*",
                            parse_mode="Markdown"
                        )
                        if play_messages is None:
                            play_messages = []
                        play_messages.append(next_msg)
                    except Exception: pass
                
        # Autoplay if queue is STILL empty (e.g. not repeating)
        if not queue and current_song:
            try:
                bot = context.bot if context else BOT_INSTANCE
                status_msg = await bot.send_message(
                    chat_id=chat_id, 
                    text="✨ *Autoplay:* Fetching next recommended song...", 
                    parse_mode="Markdown"
                )
            except Exception:
                status_msg = None
            try:
                import re
                from ytmusicapi import YTMusic
                match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", current_song.url)
                if match:
                    video_id = match.group(1)
                    ytm = YTMusic()
                    # Run in executor to prevent blocking the async loop
                    watch_playlist = await asyncio.to_thread(ytm.get_watch_playlist, videoId=video_id, radio=True, limit=10)
                    tracks = watch_playlist.get("tracks", [])
                    
                    import random
                    # Pick a random track from the top 10 recommended to guarantee variety
                    valid_tracks = [t for t in tracks[:10] if t.get("videoId") != video_id]
                    next_track = random.choice(valid_tracks) if valid_tracks else None
                            
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
            
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
                
            if queue:
                # User requested the 'Playing' message to appear after Autoplay finishes, mimicking the /play command
                bot = context.bot if context else BOT_INSTANCE
                play_msg = await bot.send_message(
                    chat_id=chat_id,
                    text="▶️ *Playing next recommended song...*",
                    parse_mode="Markdown"
                )
                if play_messages is None:
                    play_messages = []
                play_messages.append(play_msg)

        if not queue:
            state["playing"] = False
            state["current"] = None
            if GROUP_CALL_INSTANCE:
                try:
                    await GROUP_CALL_INSTANCE.leave_call(chat_id)
                except Exception:
                    pass
            bot = context.bot if context else BOT_INSTANCE
            if bot:
                try:
                    await bot.send_message(chat_id=chat_id, text="⏹ Queue is empty. Playback stopped.")
                except Exception:
                    pass
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
                f"1. 🎸 **{escape_md(stream_data.title)}** — **{escape_md(stream_data.uploader)}**\n"
                f"🏆 *Requested by:* **{escape_md(stream_data.requested_by)}**"
            )
            dur_m = stream_data.duration // 60
            dur_s = stream_data.duration % 60
            duration_str = f"0:00 ▷ ─────────── {dur_m}:{dur_s:02d}"
            
            bot = context.bot if context else BOT_INSTANCE
            state["player_message"] = await bot.send_message(
                chat_id=chat_id,
                text=msg_text,
                parse_mode="Markdown",
                reply_markup=get_player_keyboard(state, duration_str)
            )
            
            if play_messages:
                for m in play_messages:
                    try:
                        await m.delete()
                    except Exception:
                        pass
            
            # No local files to delete since we are directly streaming!
            
        else:
            await update.effective_message.reply_text(
                "Group call is not configured correctly or bot is not an admin."
            )
    except Exception as exc:
        await update.effective_message.reply_text(f"Playback failed: {exc}")
        state["playing"] = False
        return




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

    status_msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🔎 *Searching for your song, please wait...*", 
        parse_mode="Markdown"
    )

    try:
        song = await search_song(query)
        song.requested_by = update.effective_user.first_name if update.effective_user else "Unknown"
        
        try:
            await status_msg.delete()
        except Exception:
            pass
            
        state = get_state(update.effective_chat.id)
        
        # User requested that /play ALWAYS interrupts the current song and plays immediately
        state["queue"].insert(0, song)
        
        play_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="▶️ *Playing your searched song...*", 
            parse_mode="Markdown"
        )
        
        await play_next(update.effective_chat.id, update, context, play_messages=[play_msg])
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
            "🎵 *What do you want to play?*\n\n✨ _Just type the song name below!_",
            parse_mode="Markdown",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Type song name here...")
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
    # Check if it's a reply to the prompt
    if update.message and update.message.reply_to_message:
        if "What do you want to play?" in update.message.reply_to_message.text:
            context.args = update.message.text.split()
            await play(update, context)
            return
            
    # If not a reply, but in a private chat, assume it's a search!
    if update.effective_chat.type == "private":
        context.args = update.message.text.split()
        await play(update, context)
        return
        
    # In groups, we don't want to trigger on every random message, so we just ignore.

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
    global BOT_INSTANCE
    BOT_INSTANCE = application.bot

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("play", play))
    application.add_handler(CommandHandler("queue", queue_cmd))
    application.add_handler(CommandHandler("skip", skip))
    application.add_handler(CommandHandler("now", now_playing))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("diagnostics", diagnostics_cmd))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_force_reply))

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
            # Hugging Face Spaces requires a service listening on the PORT even if we are just polling!
            from http.server import HTTPServer, BaseHTTPRequestHandler
            import threading
            
            class DummyHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Bot is running on Hugging Face!")
                    
            def run_dummy_server():
                try:
                    server = HTTPServer(("0.0.0.0", port), DummyHandler)
                    server.serve_forever()
                except Exception as e:
                    logging.error(f"Dummy server failed: {e}")
                    
            threading.Thread(target=run_dummy_server, daemon=True).start()
            application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    finally:
        if lock_fd is not None and fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


if __name__ == "__main__":
    main()
