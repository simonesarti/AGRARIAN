FROM nvcr.io/nvidia/tensorrt:25.03-py3

ENV DEBIAN_FRONTEND=noninteractive

# TensorRT, cuDNN, CUDA 12.9, and Python 3.12 are pre-installed in the base image.
# Install only system-level dependencies not already present.
RUN apt-get update && apt-get install -y \
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

WORKDIR /app

# Install PyTorch separately so this heavy layer is cached independently of app changes.
# cu126 wheels are backward-compatible with the CUDA 12.9 runtime in this image.
RUN pip install --no-cache-dir \
    torch==2.6.0+cu126 \
    torchvision==0.21.0+cu126 \
    --index-url https://download.pytorch.org/whl/cu126

COPY ./app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy project structure (filtered by ./.dockerignore)
COPY . .

# Set permissions for entire /app directory at once
RUN chmod -R 777 /app

# Set entrypoint
ENTRYPOINT ["python3", "-m", "app.main"]