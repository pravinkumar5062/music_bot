# music_bot

A simple Telegram music bot that can:
- search and queue songs with /play
- show the queue with /queue
- skip to the next song with /skip
- display the current song with /now
- clear playback with /stop

## Setup

1. Create a Telegram bot with BotFather and copy its token.
2. Create a local .env file and add your token:
   TELEGRAM_BOT_TOKEN=your_token_here
3. Keep the .env file local; Render will provide the token in production.
4. Install dependencies:
   pip install -r requirements.txt
5. Run the bot:
   python bot.py

## Notes
- The bot now tries to play tracks in a Telegram voice chat when the group-call stack is available.
- If the voice-chat stack is not installed or cannot join the chat, it falls back to sending the audio file back to the chat.
- On Render, install the updated dependencies from requirements.txt and set TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_BOT_TOKEN in the environment.
