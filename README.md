# music_bot

A simple Telegram music bot that can:
- search and queue songs with /play
- show the queue with /queue
- skip to the next song with /skip
- display the current song with /now
- clear playback with /stop

## Setup

1. Create a Telegram bot with BotFather and copy its token.
2. Create a Telegram API app at my.telegram.org and copy API_ID and API_HASH.
3. Create a local .env file and add:
   TELEGRAM_BOT_TOKEN=your_bot_token
   TELEGRAM_API_ID=your_api_id
   TELEGRAM_API_HASH=your_api_hash
4. Keep the .env file local; Render will provide the bot token and app credentials in production.
5. Install dependencies:
   pip install -r requirements.txt
6. Run the bot:
   python bot.py

## Notes
- The bot now tries to play tracks in a Telegram voice chat when the group-call stack is available.
- If the voice-chat stack is not installed or cannot join the chat, it falls back to sending the audio file back to the chat.
- On Render, install the updated dependencies from requirements.txt and set TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_BOT_TOKEN in the environment.
