# 番茄小说下载器 —— 群晖 NAS Docker 网页版（端口 6080）

把 `FanqieNovelDownloader-tauri-linux-amd64.deb`（Rust + Tauri v2 桌面程序）装进 Docker，
在容器里的虚拟显示器中运行，再用 **noVNC** 把画面变成网页。
浏览器打开就能直接操作原版界面，下载的小说存到 NAS 文件夹。

```
浏览器 ──6080──> noVNC ──> x11vnc ──> Xvfb 虚拟屏幕 ──> fanqie-desktop（原版程序）
```

## 文件说明

| 文件 | 作用 |
|---|---|
| `fanqie-desktop.deb` | 原版安装包（v2026.9.30-1510），构建时安装进镜像 |
| `Dockerfile` | Ubuntu 24.04 + WebKitGTK + Xvfb + x11vnc + noVNC + 中文字体 + fcitx5 拼音 |
| `start.sh` | 启动虚拟屏幕、VNC、网页服务、拼音输入法和下载器；程序被关掉会自动重开 |
| `openbox-rc.xml` | 让程序窗口无边框铺满整个网页 |
| `docker-compose.yml` | 部署配置（端口、目录、密码） |

## 部署步骤（DSM 7.2+，Container Manager）

1. **上传**：File Station 在 `docker` 下新建 `fanqie`，把本文件夹全部内容传进去，再新建 `config` 子文件夹：
   ```
   /volume1/docker/fanqie/
   ├── docker-compose.yml  Dockerfile  start.sh  openbox-rc.xml  fanqie-desktop.deb
   └── config/
   ```
   下载目录默认是 `/volume2/downloads/小说`，没有就先建好，或在 compose 里改成你自己的路径。
2. **改密码**：编辑 `docker-compose.yml`，把 `VNC_PASSWORD=123456` 改成你自己的（VNC 密码最多 8 位）。
3. **建项目**：Container Manager → 项目 → 新增 → 选该文件夹 → 用现有 compose → 完成。
   第一次会**构建镜像**（下 Ubuntu + 装 WebKit 和中文字体，apt 走清华镜像），约 **3~6 分钟**。
   日志出现 `==> 番茄小说下载器已启动` 即就绪。
4. **用**：浏览器打开 `http://群晖IP:6080/?autoconnect=1&resize=scale`，输入密码即可看到下载器界面。
   直接打开 `http://群晖IP:6080/` 再点「连接」也行。

## 打字 / 输入中文

容器里内置了**拼音输入法**（浏览器经 VNC 只能传按键，你电脑上的输入法打不进去）：

- 点输入框后按 **Ctrl + 空格** 切换中文/英文
- 中文模式下直接敲拼音，例如 `doupo` → 按数字键选字（空格选第一个），和电脑上的拼音输入法一样
- 也可以用 noVNC 左侧小抽屉的「剪贴板」：把书名粘进去，再在程序输入框里按 Ctrl+V

> 旧版本「一打字就卡死」的原因：中文环境下程序会去找 XIM 输入法服务，容器里没有，按键就卡住了。
> 新版已修复，记得按下面「更新」的步骤重新构建。

## 第一次使用：设置保存目录

进入 **设置 → 默认保存目录 → 选择目录**，弹窗默认就在 `/downloads`，直接点 **「选择当前目录」**。
`/downloads` 就是 NAS 上的 `/volume2/downloads/小说`，下好的 TXT / EPUB 在 File Station 里就能看到。

程序的设置、番茄账号登录态、书架、历史都存在 `config/` 里，重启/重建容器都不会丢。

## 常用调整

- **端口冲突**：改 compose 里 `"6080:6080"` 左边的数字。
- **画面大小**：改 `SCREEN_WIDTH` / `SCREEN_HEIGHT`（如手机用 `1080`×`1920`），重建项目生效。
  网页左侧小抽屉 → 设置 → 缩放模式可以选「本地缩放」自适应浏览器窗口。
- **程序被误关**：点了右上角 ✕ 也没关系，3 秒后自动重新打开。
- **更新版本**：把新版 `.deb` 改名成 `fanqie-desktop.deb` 覆盖，Container Manager 里项目「停止 → 构建 → 启动」。
- **白屏/崩溃**：确认 compose 里有 `shm_size: "512mb"`；查看容器日志排查。
- **下载的文件属主是 root**：容器以 root 运行；File Station 管理员账号可正常读写/删除。

## 安全提醒

6080 端口能完全操控程序（含你的番茄账号），务必设置 `VNC_PASSWORD`，只在家庭内网 / VPN 使用，不要转发到公网。
仅限 amd64（x86_64）群晖；ARM 机型无法运行此安装包。下载内容仅供个人使用，遵守平台服务条款。
