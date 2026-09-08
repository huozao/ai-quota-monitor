FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
ENV DISPLAY=:101 DISPLAY_WIDTH=1366 DISPLAY_HEIGHT=768 DISPLAY_DEPTH=24
WORKDIR /app

RUN sed -i \
    -e 's|http://archive.ubuntu.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g' \
    -e 's|http://security.ubuntu.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g' \
    /etc/apt/sources.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc novnc websockify curl procps fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://dl.google.com/linux/linux_signing_key.pub -o /usr/share/keyrings/google-chrome.asc \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.asc] https://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/google-chrome-stable /usr/bin/webdock-chrome

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY quota_monitor/ quota_monitor/
COPY docker/ docker/
RUN mkdir -p /app/quota_browser_data /app/quota_data /app/quota_logs /app/.quota-vnc

EXPOSE 8001 6082
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -fsS http://localhost:8001/healthz || exit 1
ENTRYPOINT ["/app/docker/quota-entrypoint.sh"]
