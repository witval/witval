# 番茄小说下载器（Tauri 桌面版）→ Docker 网页版
# 原理：虚拟显示器 Xvfb 里运行桌面程序，x11vnc + noVNC 把画面转成网页，浏览器直接操作。
FROM ubuntu:24.04

ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8 \
    TZ=Asia/Shanghai \
    DISPLAY=:0 \
    HOME=/config \
    XDG_CONFIG_HOME=/config/.config \
    XDG_DATA_HOME=/config/.local/share \
    XDG_CACHE_HOME=/config/.cache \
    XDG_DOWNLOAD_DIR=/downloads \
    WEBKIT_DISABLE_COMPOSITING_MODE=1 \
    WEBKIT_DISABLE_DMABUF_RENDERER=1 \
    LIBGL_ALWAYS_SOFTWARE=1 \
    NO_AT_BRIDGE=1

COPY fanqie-desktop.deb /tmp/fanqie-desktop.deb

RUN sed -i -E "s#https?://(archive|security)\.ubuntu\.com#http://${APT_MIRROR}#g" /etc/apt/sources.list.d/ubuntu.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      /tmp/fanqie-desktop.deb \
      xvfb x11vnc openbox novnc python3-websockify \
      fonts-noto-cjk fonts-noto-color-emoji locales tzdata ca-certificates \
      dbus-x11 xdg-user-dirs gvfs libgl1-mesa-dri procps \
 && locale-gen zh_CN.UTF-8 \
 && ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html \
 && rm -rf /tmp/fanqie-desktop.deb /var/lib/apt/lists/*

COPY openbox-rc.xml /etc/openbox-fanqie.xml
COPY start.sh /start.sh
RUN chmod +x /start.sh && mkdir -p /config /downloads

VOLUME ["/config", "/downloads"]
EXPOSE 6080
CMD ["/start.sh"]
