# Use a simple Python runtime as image
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt update && apt install -y \
    ffmpeg \
    libx264-dev \
    libavcodec-dev \
    libavformat-dev \
    libavutil-dev \
    libswscale-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY ./requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy project structure (filtered by ./.dockerignore)
COPY . .

# Set permissions for entire /app directory at once
RUN chmod -R 777 /app

# set default environmental variables
ENV STREAM_PROTOCOL="webrtc"
ENV STREAM_HOST="0.0.0.0"
ENV STREAM_PORT=8889
ENV STREAM_NAME="annot"
ENV STREAM_STUN_SERVER="stun:stun.l.google.com:19302"

ENV WEBSOCKET_HOST="0.0.0.0"
ENV WEBSOCKET_PORT=8443
ENV WEBSOCKET_RECONNECTION_DELAY=5
ENV WEBSOCKET_PING_INTERVAL=30
ENV WEBSOCKET_PING_TIMEOUT=10

ENV ALERTS_REFRESH=1.0
ENV ALERTS_BOX_COLOR_TIMEDIFF=5.0
ENV ALERTS_MAX_DISPLAYED=5

ENV LOGO="assets/leonardo.png"
ENV LOGO_WIDTH=200
ENV HTML_HEIGHT=600
ENV ALERT_HEIGHT=600

# Input port:
# 8501/tcp: streamlit web UI
EXPOSE 8501/tcp

# Run the application
CMD ["streamlit", "run", "ui.py"]
