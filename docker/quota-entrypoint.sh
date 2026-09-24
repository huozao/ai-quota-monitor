#!/usr/bin/env bash
set -euo pipefail

mkdir -p /app/quota_browser_data /app/quota_data /app/quota_logs /app/.quota-vnc
rm -f /app/quota_browser_data/SingletonLock /app/quota_browser_data/SingletonSocket /app/quota_browser_data/SingletonCookie
x11vnc -storepasswd "${VNC_PASSWORD:-changeme}" /app/.quota-vnc/passwd >/dev/null 2>&1

display_number=101
display_socket="/tmp/.X11-unix/X${display_number}"
display_lock="/tmp/.X${display_number}-lock"

# A forced container stop can leave Xvfb's lock and socket behind in the
# container writable layer.  A socket alone is not proof that an X server is
# alive; refuse to remove the files only when the lock belongs to a live
# non-zombie process.
if [ -f "$display_lock" ]; then
  lock_pid="$(tr -d '[:space:]' < "$display_lock" || true)"
  lock_stat="$(ps -p "$lock_pid" -o stat= 2>/dev/null | tr -d '[:space:]' || true)"
  case "$lock_stat" in
    ''|Z*) ;;
    *)
      echo "Xvfb :${display_number} already active (pid ${lock_pid})" >&2
      exit 1
      ;;
  esac
fi
rm -f "$display_lock" "$display_socket"

Xvfb ":${display_number}" -screen 0 "${DISPLAY_WIDTH:-1366}x${DISPLAY_HEIGHT:-768}x${DISPLAY_DEPTH:-24}" -ac -nolisten tcp +extension GLX >/app/quota_logs/xvfb.log 2>&1 &
xvfb_pid=$!
xvfb_ready=0
i=0
while [ "$i" -lt 50 ]; do
  xvfb_stat="$(ps -p "$xvfb_pid" -o stat= 2>/dev/null | tr -d '[:space:]' || true)"
  case "$xvfb_stat" in
    ''|Z*) break ;;
  esac
  if [ -S "$display_socket" ]; then
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
# 支持单账号（默认全屏、端口 9224）与多账号（分屏、独立子目录与端口）动态拉起。
python3 -c '
from quota_monitor.core import get_chrome_instances, resolve_accounts
import os
accounts = resolve_accounts(
    raw_accounts=os.getenv("QUOTA_ACCOUNTS"),
    raw_codex_accounts=os.getenv("QUOTA_CODEX_ACCOUNTS"),
    x_account=os.getenv("QUOTA_X_ACCOUNT", "thsottiaux").strip().lstrip("@")
)
instances = get_chrome_instances(accounts, os.getenv("QUOTA_PROFILE_DIR", "/app/quota_browser_data"))
for inst in instances:
    urls_str = " ".join(inst["urls"])
    row = [str(inst["port"]), inst["profile_dir"], inst["pos"], inst["size"], urls_str]
    print("\t".join(row))
' | while IFS=$'\t' read -r port profile_dir pos size urls; do
  mkdir -p "$profile_dir"
  rm -f "${profile_dir}/SingletonLock" "${profile_dir}/SingletonSocket" "${profile_dir}/SingletonCookie"
  chrome_args=(
    /usr/bin/webdock-chrome
    "--user-data-dir=${profile_dir}"
    --remote-debugging-address=127.0.0.1
    "--remote-debugging-port=${port}" --no-first-run --no-default-browser-check \
    --disable-session-crashed-bubble --disable-breakpad --disable-crash-reporter \
    --disable-dev-shm-usage --no-sandbox \
    "--window-position=${pos}" \
    "--window-size=${size}" \
    --disable-blink-features=AutomationControlled
  )
  if [ -n "${CHROME_PROXY_SERVER:-}" ]; then
    chrome_args+=("--proxy-server=${CHROME_PROXY_SERVER}")
  fi
  read -r -a url_array <<< "$urls"
  chrome_args+=("${url_array[@]}")

  if [ "$port" = "9224" ]; then
    log_file="/app/quota_logs/chrome.log"
  else
    log_file="/app/quota_logs/chrome_${port}.log"
  fi
  "${chrome_args[@]}" >"$log_file" 2>&1 &
done

exec uvicorn quota_monitor.app:app --host 0.0.0.0 --port 8001
