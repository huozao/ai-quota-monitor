#!/usr/bin/env bash
set -euo pipefail

mkdir -p /app/quota_browser_data /app/quota_data /app/quota_logs /app/.quota-vnc
rm -f /app/quota_browser_data/SingletonLock /app/quota_browser_data/SingletonSocket /app/quota_browser_data/SingletonCookie
x11vnc -storepasswd "${VNC_PASSWORD:-changeme}" /app/.quota-vnc/passwd >/dev/null 2>&1

Xvfb :101 -screen 0 "${DISPLAY_WIDTH:-1366}x${DISPLAY_HEIGHT:-768}x${DISPLAY_DEPTH:-24}" -ac -nolisten tcp +extension GLX >/app/quota_logs/xvfb.log 2>&1 &
xvfb_ready=0
i=0
while [ "$i" -lt 50 ]; do
  if [ -S /tmp/.X11-unix/X101 ]; then
    xvfb_ready=1
    break
  fi
  i=$((i + 1))
  sleep 0.2
done
if [ "$xvfb_ready" -ne 1 ]; then
  echo "Xvfb :101 did not become ready" >&2
  exit 1
fi
x11vnc -display :101 -forever -shared -rfbport 5902 -xkb -rfbauth /app/.quota-vnc/passwd >/app/quota_logs/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc/ "${QUOTA_NOVNC_PORT:-6082}" localhost:5902 >/app/quota_logs/novnc.log 2>&1 &

# 普通 Chrome 进程；手工登录阶段不启动 Playwright/CDP attach。
chrome_args=(
  /usr/bin/webdock-chrome
  --user-data-dir=/app/quota_browser_data
  --remote-debugging-address=127.0.0.1
  --remote-debugging-port=9224 --no-first-run --no-default-browser-check \
  --disable-session-crashed-bubble --disable-breakpad --disable-crash-reporter \
  --disable-dev-shm-usage --no-sandbox --window-size=1366,768 \
  --disable-blink-features=AutomationControlled
)
if [ -n "${CHROME_PROXY_SERVER:-}" ]; then
  chrome_args+=("--proxy-server=${CHROME_PROXY_SERVER}")
fi
chrome_args+=("https://chatgpt.com/codex" "https://claude.ai/")
"${chrome_args[@]}" >/app/quota_logs/chrome.log 2>&1 &

exec uvicorn quota_monitor.app:app --host 0.0.0.0 --port 8001
