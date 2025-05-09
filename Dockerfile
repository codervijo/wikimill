# Use a stable Debian base image
FROM debian:stable

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    git \
    bash \
    python3 \
    python3-pip \
    python3-dev \
    python3-venv \
    libpq-dev \
    build-essential \
    libxml2-dev \
    libxslt1-dev \
    zlib1g-dev \
    libssl-dev \
    libffi-dev \
    libsasl2-dev \
    libldap2-dev \
    wget \
    gnupg2 \
    unzip \
    lsb-release \
    && rm -rf /var/lib/apt/lists/*

# Install virtualenv for creating isolated Python environments
RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

# Install Scrapy and additional Python dependencies inside the virtual environment
RUN pip install --no-cache-dir scrapy selenium googlemaps beautifulsoup4==4.12.2 fake-useragent
RUN pip install scrapy-playwright selenium fake_useragent
RUN apt-get install -y tidy

# Install Ollama
RUN apt-get update && apt-get install -y pciutils lshw
RUN curl -fsSL https://ollama.com/install.sh | sh

# Install Google Chrome for Selenium (headless mode)
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | tee /usr/share/keyrings/google-linux-signing-key.asc \
    && DISTRO=$(lsb_release -c | awk '{print $2}') \
    && echo "deb [signed-by=/usr/share/keyrings/google-linux-signing-key.asc] https://dl.google.com/linux/chrome/deb/ stable main" | tee /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable

# Install ChromeDriver for Selenium
RUN wget -q https://chromedriver.storage.googleapis.com/113.0.5672.63/chromedriver_linux64.zip \
    && unzip chromedriver_linux64.zip -d /usr/local/bin \
    && rm chromedriver_linux64.zip

# Set up app directory
WORKDIR /usr/src/app

# Expose ports for debugging (e.g., Scrapy logs or API if running as a service)
EXPOSE 6800

# Default command to run Scrapy spider (adjust spider name as needed)
#ENTRYPOINT ["scrapy", "crawl", "your_spider_name"]
