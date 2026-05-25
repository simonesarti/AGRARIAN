FROM nvidia/cuda:13.0.0-cudnn-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies (including Python, which is not in the nvidia/cuda base)
RUN apt update && apt install -y \
    python3 \
    python3-pip \
    ffmpeg \
    libx264-dev \
    libavcodec-dev \
    libavformat-dev \
    libavutil-dev \ 
    libswscale-dev \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libtbb-dev \
    libjpeg-turbo8-dev \
    libpng-dev \
    libtiff-dev \
    libgoogle-perftools-dev \
    libglib2.0-0 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV LD_PRELOAD="/usr/lib/x86_64-linux-gnu/libtcmalloc.so"

# Set the working directory
WORKDIR /app

# Install PyTorch separately so this heavy layer is cached independently of app changes
RUN pip install --no-cache-dir \
    torch==2.12.0+cu130 \
    torchvision==0.27.0+cu130 \
    --index-url https://download.pytorch.org/whl/cu130

# Copy requirements and install remaining Python dependencies
COPY ./requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy project structure (filtered by ./.dockerignore)
COPY . .

# Set permissions for entire /app directory at once
RUN chmod -R 777 /app

# open https port for ui to connect to websocket server (alerts)
EXPOSE 8443

# Set entrypoint
ENTRYPOINT ["python3", "main.py"]