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
- This version downloads the audio file from YouTube and sends it back to the chat.
- For a production-grade voice chat experience, you would need a full audio streaming setup with Telegram voice chat support.
