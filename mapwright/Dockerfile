# Use Ubuntu as the base image
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies, browser dependencies, and Python
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    git \
    ca-certificates \
    gnupg \
    libnss3 \
    libatk-bridge2.0-0 \
    libxss1 \
    libasound2 \
    libx11-xcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxrandr2 \
    libgbm-dev \
    libgtk-3-0 \
    libgtk-4-1 \
    libu2f-udev \
    libgl1 \
    libglib2.0-0 \
    libenchant-2-2 \
    libwoff1 \
    libopus0 \
    libwebpdemux2 \
    libwebpmux3 \
    libgdk-pixbuf2.0-0 \
    libevent-2.1-7 \
    libvpx7 \
    libopenjp2-7 \
    libxslt1.1 \
    fonts-liberation \
    fonts-ipafont-gothic \
    libvulkan1 \
    xdg-utils \
    locales \
    unzip \
    make \
    build-essential \
    python3 \
    python3-pip \
    python3-venv \
    x11-apps

RUN apt-get update && apt-get install -y \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-gl \
    gstreamer1.0-gtk3 \
    gstreamer1.0-pulseaudio \
    libgstreamer1.0-0 \
    libgraphene-1.0-0 \
    libflite1 \
    libavif13 \
    libharfbuzz-icu0 \
    libsecret-1-0 \
    libhyphen0 \
    libmanette-0.2-0 \
    libgles2 \
    libx264-163

# Set locale
RUN locale-gen en_US.UTF-8
ENV LANG=en_US.UTF-8
ENV LANGUAGE=en_US:en
ENV LC_ALL=en_US.UTF-8

# Install Node.js and pnpm
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get update && apt-get install -y nodejs && \
    corepack enable && corepack prepare pnpm@latest --activate

# Install uv (latest version from GitHub releases)
RUN curl -sSL https://astral.sh/uv/install.sh | bash && cp /root/.local/bin/uv /usr/local/bin

# Add uv to PATH (assumes default install path ~/.cargo/bin/uv)
ENV PATH="/root/.local/bin:$PATH"

# Set working directory for your app
WORKDIR /usr/src/app/

# Copy app files
#COPY . /usr/src/app/workspace

# Set up pnpm
RUN SHELL=/bin/bash pnpm setup
ENV PATH="$(pnpm bin -g):$PATH"

# Install Node dependencies
ENV CI=true
RUN pnpm install

# Install Playwright and Chromium
RUN pnpm exec playwright install --with-deps chromium

# Optional: Set display if using headed browser (e.g., with xvfb)
ENV DISPLAY=:0

# Set working directory
WORKDIR /usr/src/app

# Default command
CMD ["pnpm", "test"]
