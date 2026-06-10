# Use official Python image
FROM python:3.10-slim

# Install FFmpeg and required dependencies
RUN apt-get update && \
    apt-get install -y ffmpeg gcc && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirement files first to cache them
COPY requirements.txt .

# Install python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Hugging Face Spaces require the app to bind to port 7860
# Our bot already checks for the PORT environment variable
ENV PORT=7860

# Run the bot
CMD ["python", "bot.py"]
