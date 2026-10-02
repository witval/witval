#!/bin/bash
# 启动顺序：虚拟显示器 → 窗口管理器 → VNC → noVNC 网页 → 拼音输入法 → 番茄下载器（崩了自动重开）
set -e
W=${SCREEN_WIDTH:-1280}; H=${SCREEN_HEIGHT:-800}

mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME" /downloads
# 让程序的「下载」目录指向 /downloads（挂载到 NAS）
printf 'XDG_DOWNLOAD_DIR="/downloads"\n' > "$XDG_CONFIG_HOME/user-dirs.dirs"
ln -sfn /downloads "$HOME/Downloads" 2>/dev/null || true
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

eval "$(dbus-launch --sh-syntax)"

# 输入法：浏览器经 VNC 只能传按键，打不了中文，所以在容器里跑 fcitx5 拼音（Ctrl+空格 切换中英文）。
# 注意不能让 GTK 自己选输入法：中文环境下它会选 XIM，容器里没有 XIM 服务，一按键程序就卡死。
if [ "${INPUT_METHOD:-pinyin}" = "pinyin" ] && command -v fcitx5 >/dev/null; then
  PROFILE="$XDG_CONFIG_HOME/fcitx5/profile"
  if [ ! -f "$PROFILE" ]; then
    mkdir -p "$(dirname "$PROFILE")"
    cat > "$PROFILE" <<'P'
[Groups/0]
Name=Default
Default Layout=us
DefaultIM=pinyin

[Groups/0/Items/0]
Name=keyboard-us
Layout=

[Groups/0/Items/1]
Name=pinyin
Layout=

[GroupOrder]
0=Default
P
  fi
  fcitx5 -d --replace >/dev/null 2>&1 || true
  for i in $(seq 1 30); do pgrep -x fcitx5 >/dev/null && break; sleep 0.2; done
  if pgrep -x fcitx5 >/dev/null; then
    export GTK_IM_MODULE=fcitx XMODIFIERS=@im=fcitx QT_IM_MODULE=fcitx
  else
    echo "拼音输入法启动失败，只能输入英文（中文可用 noVNC 剪贴板粘贴）"
  fi
fi

echo "==> 番茄小说下载器已启动，浏览器打开 http://<IP>:6080/?autoconnect=1&resize=scale"
while true; do
  fanqie-desktop || true
  echo "程序已退出，3 秒后自动重新打开..."
  sleep 3
done
