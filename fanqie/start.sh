#!/bin/bash
# 启动顺序：虚拟显示器 → 窗口管理器 → VNC → noVNC 网页 → 番茄下载器（崩了自动重开）
set -e
W=${SCREEN_WIDTH:-1280}; H=${SCREEN_HEIGHT:-800}

mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME" /downloads
# 让程序的「下载」目录指向 /downloads（挂载到 NAS）
printf 'XDG_DOWNLOAD_DIR="/downloads"\n' > "$XDG_CONFIG_HOME/user-dirs.dirs"
ln -sfn /downloads "$HOME/Downloads" 2>/dev/null || true
ln -sfn /downloads "$HOME/下载" 2>/dev/null || true
rm -f /tmp/.X0-lock /tmp/.X11-unix/X0

Xvfb :0 -screen 0 "${W}x${H}x24" -nolisten tcp &
for i in $(seq 1 50); do [ -e /tmp/.X11-unix/X0 ] && break; sleep 0.1; done
openbox --config-file /etc/openbox-fanqie.xml &

if [ -n "$VNC_PASSWORD" ]; then
  x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass >/dev/null
  VNC_AUTH="-rfbauth /tmp/vncpass"
else
  VNC_AUTH="-nopw"
fi
x11vnc -display :0 -forever -shared -quiet -localhost -rfbport 5900 $VNC_AUTH &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

echo "==> 番茄小说下载器已启动，浏览器打开 http://<IP>:6080/?autoconnect=1&resize=scale"
eval "$(dbus-launch --sh-syntax)"
while true; do
  fanqie-desktop || true
  echo "程序已退出，3 秒后自动重新打开..."
  sleep 3
done
