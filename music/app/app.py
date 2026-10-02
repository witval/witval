#!/usr/bin/env python3
"""媒体下载器（网页 GUI，群晖版）—— 两合一

标签页一：网易云音乐
- 扫码登录自己的网易云账号（官方二维码流程，不保存密码）
- 搜索歌曲，下载到 /downloads（映射到 NAS 文件夹）
- 登录态保存在 /data/cookies.json，重启容器不用重新扫码
- 只能下载当前账号有权限的歌曲，不绕过 VIP / 版权限制

标签页二：视频/音频下载（B站 / YouTube 等，基于 yt-dlp）
- 直接搜索 B站 / YouTube（也可粘贴链接），可选“原始最佳音频 / MP3 320k / FLAC / 最高画质视频”
- 需登录/会员内容可选用 /cookies/cookies.txt
"""
import base64
import codecs
import hashlib
import html
import http.cookiejar
import io
import json
import os
import random
import re
import struct
import threading
import time
import urllib.parse
import uuid

import qrcode
import requests
from Crypto.Cipher import AES
from flask import Flask, jsonify, request, send_from_directory

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DL_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
COOKIE_FILE = os.path.join(DATA_DIR, "cookies.json")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")

tasks = {}          # task_id -> {name, status, progress, message, file}
tasks_lock = threading.Lock()


# ---------------- 访问密码 ----------------

AUTH_SALT = "media-downloader-auth-v1"
PASSWORD_FILE = os.path.join(DATA_DIR, "app_password.txt")
DEFAULT_PASSWORD = "123456"


def get_password():
    try:
        with open(PASSWORD_FILE) as f:
            return f.read().strip() or DEFAULT_PASSWORD
    except OSError:
        return DEFAULT_PASSWORD


def set_password(pw):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PASSWORD_FILE, "w") as f:
        f.write(pw)


def auth_token():
    return hashlib.sha256((AUTH_SALT + get_password()).encode()).hexdigest()


def is_authed():
    return request.cookies.get("auth") == auth_token()


# 无需登录即可访问的接口（登录页本身 + 登录/状态查询）
AUTH_EXEMPT = {"/api/auth/login", "/api/auth/status"}


@app.before_request
def _auth_guard():
    p = request.path
    # 页面与静态资源放行（页面加载后由前端弹密码框），只拦 API 数据/操作
    if not p.startswith("/api/") or p in AUTH_EXEMPT:
        return
    if not is_authed():
        return jsonify({"error": "未登录或密码已更改，请重新登录", "need_auth": True}), 401


# ---------------- 网易云 weapi 客户端 ----------------

MODULUS = int(
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b72515"
    "2b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecbd"
    "a92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cf"
    "e4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7", 16)
PUBKEY = 0x10001
NONCE = b"0CoJUm6Qyw8W8jud"
IV = b"0102030405060708"


def _aes_cbc_b64(data: bytes, key: bytes) -> bytes:
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    return base64.b64encode(AES.new(key, AES.MODE_CBC, IV).encrypt(data))


def _weapi_encrypt(payload: dict, csrf: str) -> dict:
    payload = {**payload, "csrf_token": csrf}
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    seckey = "".join(random.choice("0123456789abcdef") for _ in range(16)).encode()
    params = _aes_cbc_b64(_aes_cbc_b64(text, NONCE), seckey).decode()
    rs = int(codecs.encode(seckey[::-1], "hex"), 16)
    enc_sec_key = format(pow(rs, PUBKEY, MODULUS), "x").zfill(256)
    return {"params": params, "encSecKey": enc_sec_key}


# eapi 协议：请求 AES-128-ECB 加密，响应同密钥解密（新版搜索/取地址接口用它）
EAPI_KEY = b"e82ckenh8dichen8"


def _pkcs7_pad(b: bytes) -> bytes:
    p = 16 - len(b) % 16
    return b + bytes([p]) * p


def _pkcs7_unpad(b: bytes) -> bytes:
    n = b[-1]
    return b[:-n] if 0 < n <= 16 else b


def _eapi_encrypt(api_path: str, payload: dict) -> dict:
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.md5(f"nobody{api_path}use{text}md5forencrypt".encode()).hexdigest()
    data = f"{api_path}-36cd479b6b5-{text}-36cd479b6b5-{digest}".encode()
    enc = AES.new(EAPI_KEY, AES.MODE_ECB).encrypt(_pkcs7_pad(data))
    return {"params": enc.hex().upper()}


def _eapi_decrypt(hex_text: str):
    raw = bytes.fromhex(hex_text.strip())
    dec = _pkcs7_unpad(AES.new(EAPI_KEY, AES.MODE_ECB).decrypt(raw))
    return json.loads(dec.decode("utf-8", "replace"))


# 设备 ID 编码（官方匿名注册接口要求的格式）
DEVICE_XOR_KEY = b"3go8&$8*3*3h0k(2)2"


def _encode_device_id(device_id: str) -> str:
    raw = device_id.encode()
    xored = bytes(b ^ DEVICE_XOR_KEY[i % len(DEVICE_XOR_KEY)] for i, b in enumerate(raw))
    return base64.b64encode(hashlib.md5(xored).digest()).decode()


def _as_dict(data, where=""):
    """网易云偶尔把响应/字段以 JSON 字符串返回（常见于风控/限流），统一转成字典。"""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            raise RuntimeError(f"网易云返回了非预期内容（可能被风控/限流）：{data[:120]}")
    if not isinstance(data, dict):
        raise RuntimeError(f"网易云返回了非预期类型 {type(data).__name__}（接口 {where}）")
    return data


class NCM:
    BASE = "https://music.163.com"

    def __init__(self, cookie_file):
        self.cookie_file = cookie_file
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/122.0.0.0 Safari/537.36"),
            "Referer": "https://music.163.com/",
            "Origin": "https://music.163.com",
        })
        self.load_cookies()
        self.ensure_device()

    def ensure_device(self):
        """补齐设备身份 cookie，降低扫码登录被风控（8821）的概率。"""
        try:
            # 1) 客户端标识 cookie（eapi 需要稳定的 deviceId / os / appver）
            if not self.s.cookies.get("os"):
                self.s.cookies.set("os", "pc", domain=".music.163.com")
            if not self.s.cookies.get("appver"):
                self.s.cookies.set("appver", "2.10.13", domain=".music.163.com")
            if not self.s.cookies.get("osver"):
                self.s.cookies.set("osver", "Microsoft-Windows-10", domain=".music.163.com")
            if not self.s.cookies.get("deviceId"):
                self.s.cookies.set("deviceId", uuid.uuid4().hex[:16].upper(),
                                   domain=".music.163.com")
            # 2) 访问一次官网，拿 NMTID 等基础 cookie
            if not self.s.cookies.get("NMTID"):
                try:
                    self.s.get(self.BASE, timeout=15)
                except Exception:
                    pass
            # 3) 未登录时注册匿名设备，拿游客凭证 MUSIC_A
            if not (self.s.cookies.get("MUSIC_U") or self.s.cookies.get("MUSIC_A")):
                device_id = uuid.uuid4().hex[:16].upper()
                username = base64.b64encode(
                    f"{device_id} {_encode_device_id(device_id)}".encode()).decode()
                rsp = self.weapi("/register/anonimous", {"username": username})
                if int((rsp or {}).get("code", 0)) == 200:
                    print("==> 匿名设备注册成功，已获得游客凭证")
                    self.save_cookies()
        except Exception as e:
            print(f"!! 设备初始化失败（可能更容易触发风控）: {e}")

    # ---- cookie 持久化 ----
    def save_cookies(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(self.cookie_file, "w") as f:
                json.dump(requests.utils.dict_from_cookiejar(self.s.cookies), f)
        except Exception as e:
            print(f"!! 保存登录态失败: {e}")

    def load_cookies(self):
        try:
            with open(self.cookie_file) as f:
                self.s.cookies = requests.utils.cookiejar_from_dict(json.load(f))
            print("==> 已恢复上次的登录态")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"!! 恢复登录态失败（需要重新扫码）: {e}")

    def clear_cookies(self):
        self.s.cookies.clear()
        try:
            os.remove(self.cookie_file)
        except OSError:
            pass

    # ---- 加密请求 ----
    def weapi(self, path, payload=None):
        csrf = self.s.cookies.get("__csrf", "") or ""
        url = f"{self.BASE}/weapi{path}"
        if csrf:
            url += f"?csrf_token={csrf}"
        r = self.s.post(url, data=_weapi_encrypt(payload or {}, csrf), timeout=20)
        r.raise_for_status()
        return _as_dict(r.json(), path)

    def eapi(self, api_path, payload=None):
        """eapi 接口：请求加密、响应解密。api_path 形如 /api/cloudsearch/pc。"""
        body = _eapi_encrypt(api_path, payload or {})
        url = "https://interface.music.163.com/eapi" + api_path[len("/api"):]
        r = self.s.post(url, data=body, timeout=20)
        r.raise_for_status()
        text = r.text.strip()
        # 正常是加密 hex；偶尔直接回明文 JSON，两种都兼容
        if text[:1] in "{[":
            return _as_dict(r.json(), api_path)
        try:
            return _as_dict(_eapi_decrypt(text), api_path)
        except Exception as e:
            raise RuntimeError(f"网易云返回内容无法解密（可能被风控/限流）：{text[:80]}") from e

    # ---- 业务接口 ----
    def qr_unikey(self):
        self.ensure_device()
        return self.weapi("/login/qrcode/unikey", {"type": "1"})

    def qr_check(self, key):
        rsp = self.weapi("/login/qrcode/client/login", {"key": key, "type": "1"})
        if int(rsp.get("code", 0)) == 803:
            self.save_cookies()
        return rsp

    def profile(self):
        try:
            rsp = self.weapi("/w/nuser/account/get")
            return (rsp or {}).get("profile") or {}
        except Exception:
            return {}

    def logout(self):
        try:
            self.weapi("/logout")
        except Exception:
            pass
        self.clear_cookies()

    def search(self, keyword, offset=0, limit=30):
        rsp = self.eapi("/api/cloudsearch/pc", {
            "s": keyword, "type": "1",
            "limit": str(limit), "offset": str(offset), "total": "true",
        })
        code = int(rsp.get("code", 200))
        if code != 200:
            msg = rsp.get("message") or rsp.get("msg") or ""
            raise RuntimeError(f"网易云返回错误 code={code} {msg}".strip())
        return _as_dict(rsp.get("result") or {}, "search.result")

    def song_detail(self, song_id):
        rsp = self.eapi("/api/v3/song/detail", {"c": json.dumps([{"id": int(song_id)}])})
        songs = (rsp or {}).get("songs") or []
        return songs[0] if songs and isinstance(songs[0], dict) else {}

    def song_url(self, song_id, level="exhigh"):
        rsp = self.eapi("/api/song/enhance/player/url/v1", {
            "ids": json.dumps([int(song_id)]),
            "level": level,
            "encodeType": "flac",
        })
        info = ((rsp or {}).get("data") or [{}])[0]
        if isinstance(info, dict) and info.get("url"):
            return info
        # 兜底：老接口按码率取
        br = {"standard": 128000, "exhigh": 320000, "lossless": 999000}.get(level, 320000)
        rsp = self.eapi("/api/song/enhance/player/url", {
            "ids": f"[{int(song_id)}]", "br": str(br),
        })
        info = ((rsp or {}).get("data") or [{}])[0]
        return info if isinstance(info, dict) else {}


ncm = NCM(COOKIE_FILE)


# ---------------- 页面 ----------------

@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


# ---------------- 访问密码接口 ----------------

@app.get("/api/auth/status")
def api_auth_status():
    return jsonify({"authed": is_authed(), "is_default": get_password() == DEFAULT_PASSWORD})


@app.post("/api/auth/login")
def api_auth_login():
    data = request.get_json(silent=True) or {}
    if data.get("password") == get_password():
        resp = jsonify({"ok": True})
        resp.set_cookie("auth", auth_token(), max_age=30 * 86400, httponly=True, samesite="Lax")
        return resp
    return jsonify({"error": "密码错误"}), 403


@app.post("/api/auth/change")
def api_auth_change():
    data = request.get_json(silent=True) or {}
    if data.get("old") != get_password():
        return jsonify({"error": "原密码错误"}), 403
    new = (data.get("new") or "").strip()
    if len(new) < 4:
        return jsonify({"error": "新密码至少 4 位"}), 400
    set_password(new)
    resp = jsonify({"ok": True, "message": "密码已修改"})
    resp.set_cookie("auth", auth_token(), max_age=30 * 86400, httponly=True, samesite="Lax")
    return resp


@app.post("/api/auth/logout")
def api_auth_logout():
    resp = jsonify({"ok": True})
    resp.delete_cookie("auth")
    return resp


# ---------------- 登录 ----------------

def login_profile():
    p = ncm.profile()
    if p.get("userId"):
        return {"logged_in": True,
                "nickname": p.get("nickname", ""),
                "avatar": p.get("avatarUrl", ""),
                "vip": bool(p.get("vipType"))}
    return {"logged_in": False}


@app.get("/api/status")
def api_status():
    return jsonify(login_profile())


@app.get("/api/qr/new")
def api_qr_new():
    try:
        key = ncm.qr_unikey().get("unikey", "")
        if not key:
            return jsonify({"error": "获取二维码失败，请重试"}), 500
        url = f"https://music.163.com/login?codekey={key}"
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        return jsonify({"key": key, "qr": f"data:image/png;base64,{b64}"})
    except Exception as e:
        return jsonify({"error": f"生成二维码失败：{e}"}), 500


@app.get("/api/qr/check")
def api_qr_check():
    key = request.args.get("key", "")
    if not key:
        return jsonify({"error": "缺少 key"}), 400
    try:
        rsp = ncm.qr_check(key)
        code = int(rsp.get("code", 0))
        msg = {800: "二维码已过期，请刷新",
               801: "等待扫码...",
               802: "已扫码，请在手机上确认",
               803: "登录成功",
               8821: "被网易云风控拦截(8821)：请点「刷新二维码」重试；"
                     "若反复出现，等 10~30 分钟或在手机 App 里先随便听一首歌再试"}.get(code)
        if msg is None:
            server_msg = rsp.get("message") or rsp.get("msg") or ""
            msg = f"登录失败({code})：{server_msg}".rstrip("：")
        return jsonify({"code": code, "message": msg})
    except Exception as e:
        return jsonify({"error": f"检查扫码状态失败：{e}"}), 500


@app.post("/api/logout")
def api_logout():
    ncm.logout()
    return jsonify({"ok": True, "message": "已退出登录，刷新页面重新扫码"})


# ---------------- 搜索 ----------------

@app.get("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    offset = int(request.args.get("offset", 0))
    if not q:
        return jsonify({"error": "请输入搜索关键词"}), 400
    try:
        result = ncm.search(q, offset=offset)
        songs = []
        for s in result.get("songs") or []:
            if not isinstance(s, dict):
                continue
            al = s.get("al") or {}
            songs.append({
                "id": s.get("id"),
                "name": s.get("name", ""),
                "artists": " / ".join(a.get("name", "") for a in (s.get("ar") or [])),
                "album": al.get("name", ""),
                "pic": al.get("picUrl", ""),
                "duration": int(s.get("dt", 0)) // 1000,
                "fee": s.get("fee", 0),   # 1 = VIP 歌曲
            })
        return jsonify({"songs": songs, "total": result.get("songCount", len(songs))})
    except Exception as e:
        return jsonify({"error": f"搜索失败：{e}"}), 500


# ---------------- 下载 ----------------

def sanitize(name):
    return re.sub(r'[\\/:*?"<>|\r\n]', "_", name).strip() or "未命名"


def tag_file(path, ext, title, artists, album, pic_url):
    """写入歌曲标签和封面，失败不影响下载结果。"""
    try:
        cover = None
        if pic_url:
            try:
                cover = requests.get(f"{pic_url}?param=500y500", timeout=15).content
            except Exception:
                cover = None
        if ext == "mp3":
            from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, ID3NoHeaderError
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.add(TIT2(encoding=3, text=title))
            tags.add(TPE1(encoding=3, text=artists))
            tags.add(TALB(encoding=3, text=album))
            if cover:
                tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
            tags.save(path)
        elif ext == "flac":
            from mutagen.flac import FLAC, Picture
            f = FLAC(path)
            f["title"] = title
            f["artist"] = artists
            f["album"] = album
            if cover:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.data = cover
                f.add_picture(pic)
            f.save()
    except Exception as e:
        print(f"!! 写入标签失败（不影响文件）: {e}")


def set_task(task_id, **kw):
    with tasks_lock:
        tasks[task_id].update(**kw)


def download_worker(task_id, song_id, level):
    try:
        set_task(task_id, status="running", message="获取歌曲信息...")
        song = ncm.song_detail(song_id)
        title = song.get("name", str(song_id))
        artists = " / ".join(a.get("name", "") for a in (song.get("ar") or [])) or "未知歌手"
        al = song.get("al") or {}
        set_task(task_id, name=f"{artists} - {title}")

        set_task(task_id, message="获取下载地址...")
        info = ncm.song_url(song_id, level)
        url = info.get("url")
        if not url:
            set_task(task_id, status="error",
                     message="无下载权限：该歌曲可能需要 VIP、需单独购买或无版权（不支持绕过）")
            return

        ext = (info.get("type") or "mp3").lower()
        fname = sanitize(f"{artists} - {title}") + f".{ext}"
        os.makedirs(DL_DIR, exist_ok=True)
        path = os.path.join(DL_DIR, fname)

        set_task(task_id, message="下载中...", file=fname)
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0))
            done = 0
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        set_task(task_id, progress=int(done * 100 / total))
            os.replace(tmp, path)

        set_task(task_id, message="写入标签...")
        tag_file(path, ext, title, artists, al.get("name", ""), al.get("picUrl", ""))

        size_mb = os.path.getsize(path) / 1024 / 1024
        set_task(task_id, status="done", progress=100,
                 message=f"完成（{size_mb:.1f} MB，{info.get('level') or ext}）")
    except Exception as e:
        set_task(task_id, status="error", message=f"下载失败：{e}")


@app.post("/api/download")
def api_download():
    data = request.get_json(silent=True) or {}
    song_id = data.get("id")
    level = data.get("level", "exhigh")
    if level not in ("standard", "exhigh", "lossless"):
        level = "exhigh"
    if not song_id:
        return jsonify({"error": "缺少歌曲 id"}), 400
    if not login_profile()["logged_in"]:
        return jsonify({"error": "请先扫码登录"}), 403
    task_id = uuid.uuid4().hex[:12]
    with tasks_lock:
        tasks[task_id] = {"id": task_id, "song_id": song_id, "name": str(song_id),
                          "status": "pending", "progress": 0, "message": "排队中...",
                          "file": "", "ts": time.time()}
    threading.Thread(target=download_worker, args=(task_id, song_id, level), daemon=True).start()
    return jsonify({"task_id": task_id})


# ---------------- 哔哩哔哩 扫码登录（用于会员 / 需登录内容） ----------------

BILI_COOKIE_FILE = os.path.join(DATA_DIR, "bili_cookies.txt")


class Bili:
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": self.UA, "Referer": "https://www.bilibili.com/"})
        self._load_cookies()
        try:
            self.s.get("https://www.bilibili.com/", timeout=15)   # 拿 buvid 等基础 cookie
        except Exception:
            pass

    def _load_cookies(self):
        if os.path.exists(BILI_COOKIE_FILE):
            try:
                cj = http.cookiejar.MozillaCookieJar(BILI_COOKIE_FILE)
                cj.load(ignore_discard=True, ignore_expires=True)
                self.s.cookies = cj
                print("==> 已恢复 B站登录态")
            except Exception as e:
                print(f"!! 恢复 B站登录态失败: {e}")

    def _save_cookies(self):
        """写成 Netscape cookies.txt，供 yt-dlp 下载会员内容时使用。"""
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            lines = ["# Netscape HTTP Cookie File"]
            for c in self.s.cookies:
                domain = c.domain or ".bilibili.com"
                if not domain.startswith("."):
                    domain = "." + domain
                expiry = str(int(c.expires)) if c.expires else "2147483647"
                secure = "TRUE" if c.secure else "FALSE"
                lines.append("\t".join([domain, "TRUE", c.path or "/", secure,
                                        expiry, c.name, c.value or ""]))
            with open(BILI_COOKIE_FILE, "w") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            print(f"!! 保存 B站登录态失败: {e}")

    # 采用 TV 端扫码登录接口（带 appkey 签名，规避网页版的 wbi 签名要求）
    APPKEY = "4409e2ce8ffd12b8"
    APPSEC = "59b43e04ad6965f34319062b478f83dd"

    def _sign(self, params):
        q = urllib.parse.urlencode(sorted(params.items()))
        params["sign"] = hashlib.md5((q + self.APPSEC).encode()).hexdigest()
        return params

    def qr_generate(self):
        params = self._sign({"appkey": self.APPKEY, "local_id": "0",
                             "ts": str(int(time.time()))})
        r = self.s.post("https://passport.bilibili.com/x/passport-tv-login/qrcode/auth_code",
                        data=params, timeout=15)
        return r.json()   # data: {url, auth_code}

    def qr_poll(self, auth_code):
        params = self._sign({"appkey": self.APPKEY, "auth_code": auth_code,
                             "local_id": "0", "ts": str(int(time.time()))})
        r = self.s.post("https://passport.bilibili.com/x/passport-tv-login/qrcode/poll",
                        data=params, timeout=15)
        data = r.json()
        if int(data.get("code", -1)) == 0:
            self._save_from_tv(data.get("data") or {})
        return data

    def _save_from_tv(self, data):
        for c in (data.get("cookie_info") or {}).get("cookies") or []:
            if c.get("name"):
                self.s.cookies.set(c["name"], c.get("value", ""), domain=".bilibili.com")
        self._save_cookies()

    def nav(self):
        try:
            d = (self.s.get("https://api.bilibili.com/x/web-interface/nav", timeout=15)
                 .json().get("data") or {})
            if d.get("isLogin"):
                vip = d.get("vip") or {}
                return {"logged_in": True, "nickname": d.get("uname", ""),
                        "vip": bool(vip.get("status")),
                        "vip_label": ((vip.get("label") or {}).get("text") or "")}
        except Exception:
            pass
        return {"logged_in": False}

    def logout(self):
        self.s.cookies.clear()
        self._mixin_key = None
        try:
            os.remove(BILI_COOKIE_FILE)
        except OSError:
            pass

    # ---- 视频搜索（网页版 wbi 签名接口）----
    WBI_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
               33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
               61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
               36, 20, 34, 44, 52]
    _mixin_key = None
    _mixin_ts = 0

    def _wbi_key(self, force=False):
        if force or not self._mixin_key or time.time() - self._mixin_ts > 3600:
            d = self.s.get("https://api.bilibili.com/x/web-interface/nav", timeout=15).json()
            wbi = (d.get("data") or {}).get("wbi_img") or {}
            img = wbi["img_url"].rsplit("/", 1)[1].split(".")[0]
            sub = wbi["sub_url"].rsplit("/", 1)[1].split(".")[0]
            raw = img + sub
            self._mixin_key = "".join(raw[i] for i in self.WBI_TAB)[:32]
            self._mixin_ts = time.time()
        return self._mixin_key

    @staticmethod
    def _wbi_sign(params, mixin_key):
        params = dict(params, wts=int(time.time()))
        params = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in sorted(params.items())}
        q = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        params["w_rid"] = hashlib.md5((q + mixin_key).encode()).hexdigest()
        return params

    def search_video(self, keyword, page=1, page_size=20):
        params = {"search_type": "video", "keyword": keyword,
                  "page": page, "page_size": page_size}
        err = ""
        for attempt in range(2):
            try:
                if attempt:   # 第二次：刷新游客 cookie 和签名密钥
                    self.s.get("https://www.bilibili.com/", timeout=15)
                signed = self._wbi_sign(params, self._wbi_key(force=bool(attempt)))
                d = self.s.get("https://api.bilibili.com/x/web-interface/wbi/search/type",
                               params=signed, timeout=15,
                               headers={"Referer": "https://search.bilibili.com/"}).json()
                if int(d.get("code", -1)) == 0:
                    out = []
                    for v in (d.get("data") or {}).get("result") or []:
                        if v.get("type") != "video" or not v.get("bvid"):
                            continue
                        pic = v.get("pic") or ""
                        if pic.startswith("//"):
                            pic = "https:" + pic
                        out.append({
                            "source": "bilibili",
                            "title": html.unescape(re.sub(r"<[^>]+>", "", v.get("title", ""))),
                            "url": f"https://www.bilibili.com/video/{v['bvid']}",
                            "uploader": v.get("author", ""),
                            "duration": _dur_to_sec(v.get("duration")),
                            "pic": (pic + "@320w_180h_1c.jpg") if pic else "",
                            "plays": v.get("play"),
                        })
                    return out
                err = f"code={d.get('code')} {d.get('message', '')}"
            except Exception as e:
                err = str(e)
        raise RuntimeError(f"B站接口：{err}")


def _dur_to_sec(text):
    try:
        sec = 0
        for part in str(text).split(":"):
            sec = sec * 60 + int(part)
        return sec
    except (TypeError, ValueError):
        return 0


bili = Bili()


@app.get("/api/bili/status")
def api_bili_status():
    return jsonify(bili.nav())


@app.get("/api/bili/qr/new")
def api_bili_qr_new():
    try:
        d = bili.qr_generate()
        data = d.get("data") or {}
        url, key = data.get("url"), data.get("auth_code")
        if not url or not key:
            return jsonify({"error": f"获取二维码失败：{d.get('message', d)}"}), 500
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return jsonify({"key": key,
                        "qr": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()})
    except Exception as e:
        return jsonify({"error": f"生成二维码失败：{e}"}), 500


@app.get("/api/bili/qr/check")
def api_bili_qr_check():
    key = request.args.get("key", "")
    if not key:
        return jsonify({"error": "缺少 key"}), 400
    try:
        rsp = bili.qr_poll(key)
        code = int(rsp.get("code", -1))
        # TV 端轮询状态码：0 成功；86039 未扫码；86090 已扫码待确认；86038 已失效
        msg = {0: "登录成功", 86039: "等待扫码...", 86090: "已扫码，请在手机上确认",
               86038: "二维码已过期，请刷新"}.get(code, rsp.get("message") or f"状态({code})")
        return jsonify({"code": code, "message": msg})
    except Exception as e:
        return jsonify({"error": f"检查扫码状态失败：{e}"}), 500


@app.post("/api/bili/logout")
def api_bili_logout():
    bili.logout()
    return jsonify({"ok": True, "message": "已退出 B站登录"})


# ---------------- 视频/音频下载（yt-dlp，B站 / YouTube 等） ----------------

COOKIE_TXT = "/cookies/cookies.txt"                       # 手动挂载（只读）
YT_COOKIE_FILE = os.path.join(DATA_DIR, "yt_cookies.txt")  # 网页上传（YouTube 等）


def _pick_cookiefile():
    """合并所有可用的 cookie 来源（手动挂载 + 上传 + B站扫码）成一个文件给 yt-dlp。"""
    sources = [p for p in (COOKIE_TXT, YT_COOKIE_FILE, BILI_COOKIE_FILE) if os.path.exists(p)]
    if not sources:
        return None
    if len(sources) == 1:
        return sources[0]
    combined = os.path.join(DATA_DIR, "combined_cookies.txt")
    try:
        with open(combined, "w") as out:
            out.write("# Netscape HTTP Cookie File\n")
            for p in sources:
                try:
                    with open(p) as fp:
                        for line in fp:
                            if line.strip() and not line.lstrip().startswith("#"):
                                out.write(line if line.endswith("\n") else line + "\n")
                except OSError:
                    pass
        return combined
    except OSError:
        return sources[0]


@app.get("/api/cookies/status")
def api_cookies_status():
    return jsonify({"manual": os.path.exists(COOKIE_TXT),
                    "youtube": os.path.exists(YT_COOKIE_FILE),
                    "bili": os.path.exists(BILI_COOKIE_FILE)})


@app.post("/api/cookies/youtube")
def api_cookies_youtube():
    data = request.get_json(silent=True) or {}
    content = data.get("content", "")
    if not isinstance(content, str) or "\t" not in content:
        return jsonify({"error": "内容不是有效的 cookies.txt（Netscape 格式）"}), 400
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(YT_COOKIE_FILE, "w") as f:
            f.write(content if content.startswith("#") else "# Netscape HTTP Cookie File\n" + content)
        return jsonify({"ok": True, "message": "YouTube cookies 已保存"})
    except OSError as e:
        return jsonify({"error": f"保存失败：{e}"}), 500


@app.post("/api/cookies/youtube/clear")
def api_cookies_youtube_clear():
    try:
        os.remove(YT_COOKIE_FILE)
    except OSError:
        pass
    return jsonify({"ok": True, "message": "已清除 YouTube cookies"})

# YouTube 国内需走代理；B站默认直连（可用 PROXY_FOR_BILIBILI=true 强制走代理）
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "").strip() or None
PROXY_FOR_BILIBILI = os.environ.get("PROXY_FOR_BILIBILI", "false").lower() == "true"
# 可选：bgutil PO Token 服务地址，解决 YouTube「Sign in to confirm you're not a bot」
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "").strip()


def _is_bili(url):
    u = url.lower()
    return "bilibili.com" in u or "b23.tv" in u


def _ytdl_base_opts(url):
    """B站/YouTube 共用的 yt-dlp 参数（代理、cookie、YouTube 防风控）。"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 5,
        "fragment_retries": 5,
        # YouTube 需要 JS 运行时（deno）解签名；没装 yt-dlp-ejs 时允许从 GitHub 拉解签脚本
        "remote_components": ["ejs:github"],
    }
    if YTDLP_PROXY and (not _is_bili(url) or PROXY_FOR_BILIBILI):
        opts["proxy"] = YTDLP_PROXY
    cookie_path = _pick_cookiefile()
    if cookie_path:
        opts["cookiefile"] = cookie_path
    if POT_PROVIDER_URL and not _is_bili(url):
        opts["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]}}
    return opts


def _ytdl_error_hint(msg, url):
    low = msg.lower()
    if _is_bili(url):
        if "cookies" in low or "login" in low or "会员" in msg or "premium" in low:
            return "（该内容可能需要登录/大会员，请先扫码登录 B站）"
        return ""
    if any(k in low for k in ("timed out", "timeout", "connection", "unreachable",
                              "getaddrinfo", "name or service", "network is", "proxy")):
        return ("（连不上 YouTube：国内需要在 docker-compose.yml 里设置 YTDLP_PROXY=http://代理IP:端口，"
                "并在代理软件里开启「允许局域网连接」）")
    if "sign in" in low or "not a bot" in low or "403" in low or "po token" in low:
        return ("（被 YouTube 风控：重启容器更新 yt-dlp；仍不行就启用 compose 里的 bgutil-pot 服务，"
                "或上传 YouTube cookies.txt）")
    if "javascript" in low or "js runtime" in low or "deno" in low or "n challenge" in low \
            or "signature" in low:
        return "（缺少 JS 运行时：确认 compose 里安装的是 yt-dlp[default,deno]，然后重启容器）"
    if "cookies" in low or "members" in low or "confirm your age" in low:
        return "（该内容需要登录，请上传 YouTube cookies.txt）"
    return ""


YTDL_MODES = {
    "audio_best": {"label": "原始最佳音频", "audio": True},
    "mp3":        {"label": "MP3 320k", "audio": True},
    "flac":       {"label": "FLAC 无损", "audio": True},
    "video":      {"label": "最高画质视频", "audio": False},
}


def _ytdl_opts(task_id, mode, url):
    def hook(d):
        st = d.get("status")
        if st == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            pct = int(done * 100 / total) if total else 0
            spd = d.get("speed") or 0
            spd_s = f"，{spd/1024/1024:.1f} MB/s" if spd else ""
            set_task(task_id, status="running", progress=pct, message=f"下载中...{spd_s}")
        elif st == "finished":
            set_task(task_id, message="转码/合并中...")

    opts = _ytdl_base_opts(url)
    opts.update({
        "outtmpl": os.path.join(DL_DIR, "%(title).150B.%(ext)s"),
        "progress_hooks": [hook],
        "restrictfilenames": False,
        "windowsfilenames": True,   # 文件名兼容 SMB / Windows
        "postprocessors": [],
    })

    if mode == "video":
        opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
    else:
        opts["format"] = "bestaudio/best"
        if mode == "mp3":
            opts["postprocessors"].append(
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"})
        elif mode == "flac":
            opts["postprocessors"].append(
                {"key": "FFmpegExtractAudio", "preferredcodec": "flac"})
    # 写入标题/上传者等元数据（需要 ffmpeg，容器已内置）
    opts["postprocessors"].append({"key": "FFmpegMetadata"})
    return opts


def ytdl_worker(task_id, url, mode):
    try:
        import yt_dlp
    except Exception:
        set_task(task_id, status="error", message="yt-dlp 未安装（请检查容器依赖）")
        return
    try:
        set_task(task_id, status="running", message="解析链接...")
        opts = _ytdl_opts(task_id, mode, url)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("_type") == "playlist" and info.get("entries"):
                info = info["entries"][0]
            title = info.get("title") or url
            set_task(task_id, name=title[:120])
            ydl.download([url])
        set_task(task_id, status="done", progress=100,
                 message=f"完成（{YTDL_MODES.get(mode, {}).get('label', mode)}）")
    except Exception as e:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        msg = re.sub(r";?\s*please report this issue.*$", "", msg, flags=re.S | re.I).strip()
        msg = msg.replace("ERROR: ", "")[:220] + _ytdl_error_hint(msg, url)
        set_task(task_id, status="error", message=f"下载失败：{msg}")


def _youtube_search(q, page, page_size=20):
    import yt_dlp
    opts = _ytdl_base_opts("https://www.youtube.com/")
    opts.update({"extract_flat": "in_playlist", "skip_download": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{page * page_size}:{q}", download=False)
    out = []
    for e in (info.get("entries") or [])[(page - 1) * page_size:]:
        if not e or not e.get("id"):
            continue
        out.append({
            "source": "youtube",
            "title": e.get("title") or e["id"],
            "url": f"https://www.youtube.com/watch?v={e['id']}",
            "uploader": e.get("channel") or e.get("uploader") or "",
            "duration": int(e.get("duration") or 0),
            "pic": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg",
            "plays": e.get("view_count"),
        })
    return out


@app.get("/api/vsearch")
def api_vsearch():
    q = request.args.get("q", "").strip()
    source = request.args.get("source", "bilibili")
    page = max(1, int(request.args.get("page", 1) or 1))
    if not q:
        return jsonify({"error": "请输入搜索关键词"}), 400
    try:
        if source == "youtube":
            items = _youtube_search(q, page)
        else:
            items = bili.search_video(q, page)
        return jsonify({"items": items})
    except Exception as e:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        msg = re.sub(r";?\s*please report this issue.*$", "", msg, flags=re.S | re.I).strip()
        hint = _ytdl_error_hint(msg, "https://www.youtube.com/") if source == "youtube" else ""
        return jsonify({"error": f"搜索失败：{msg.replace('ERROR: ', '')[:200]}{hint}"}), 500


THUMB_HOSTS = ("ytimg.com", "ggpht.com", "hdslb.com", "biliimg.com")


@app.get("/api/thumb")
def api_thumb():
    """封面代理：浏览器不一定能直连 YouTube 图片，B站图片有防盗链，统一由服务器转发。"""
    u = request.args.get("u", "")
    host = (urllib.parse.urlparse(u).hostname or "").lower()
    if not u.startswith("https://") or not any(host == h or host.endswith("." + h)
                                                for h in THUMB_HOSTS):
        return "", 404
    proxies = None
    if YTDLP_PROXY and ("ytimg" in host or "ggpht" in host or PROXY_FOR_BILIBILI):
        proxies = {"http": YTDLP_PROXY, "https": YTDLP_PROXY}
    try:
        r = requests.get(u, timeout=15, proxies=proxies,
                         headers={"User-Agent": Bili.UA, "Referer": "https://www.bilibili.com/"})
        if r.status_code != 200:
            return "", 404
        return r.content, 200, {"Content-Type": r.headers.get("Content-Type", "image/jpeg"),
                                "Cache-Control": "public, max-age=86400"}
    except Exception:
        return "", 404


@app.get("/api/ytdl/modes")
def api_ytdl_modes():
    try:
        import yt_dlp
        ver = yt_dlp.version.__version__
    except Exception:
        ver = "未安装"
    import shutil
    return jsonify({"modes": [{"key": k, "label": v["label"]} for k, v in YTDL_MODES.items()],
                    "has_cookies": _pick_cookiefile() is not None,
                    "ytdlp": ver, "deno": bool(shutil.which("deno")),
                    "proxy": bool(YTDLP_PROXY), "pot": bool(POT_PROVIDER_URL)})


@app.post("/api/ytdl")
def api_ytdl():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    mode = data.get("mode", "audio_best")
    if mode not in YTDL_MODES:
        mode = "audio_best"
    if not re.match(r"^https?://", url):
        return jsonify({"error": "请输入正确的链接（以 http/https 开头）"}), 400
    task_id = uuid.uuid4().hex[:12]
    with tasks_lock:
        tasks[task_id] = {"id": task_id, "name": url[:80],
                          "status": "pending", "progress": 0, "message": "排队中...",
                          "file": "", "ts": time.time()}
    threading.Thread(target=ytdl_worker, args=(task_id, url, mode), daemon=True).start()
    return jsonify({"task_id": task_id})


@app.get("/api/tasks")
def api_tasks():
    with tasks_lock:
        items = sorted(tasks.values(), key=lambda t: t["ts"], reverse=True)[:50]
        return jsonify({"tasks": items})


@app.get("/api/files")
def api_files():
    try:
        files = []
        for n in sorted(os.listdir(DL_DIR)):
            p = os.path.join(DL_DIR, n)
            if os.path.isfile(p) and not n.endswith(".part"):
                files.append({"name": n, "size_mb": round(os.path.getsize(p) / 1024 / 1024, 1)})
        return jsonify({"files": files})
    except FileNotFoundError:
        return jsonify({"files": []})


# ---------------- NCM → FLAC/MP3 解密转换 ----------------
# 网易云的加密格式 .ncm 内部其实就是 flac 或 mp3，解密后即得原始无损/有损文件。
# 放进 downloads 的 .ncm 会被自动扫描并转换（原理同 freelrc 的 NCM-to-FLAC，不做转码）。

NCM_CORE_KEY = bytes.fromhex("687A4852416D736F356B496E62617857")   # hzHRAmso5kInbaxW
NCM_META_KEY = bytes.fromhex("2331346C6A6B5F215C5D2630553C2728")   # #14ljk_!\]&0U<'(


def _ncm_key_box(key: bytes) -> bytearray:
    box = bytearray(range(256))
    c = last = off = 0
    klen = len(key)
    for i in range(256):
        swap = box[i]
        c = (swap + last + key[off]) & 0xff
        box[i] = box[c]
        box[c] = swap
        last = c
        off = (off + 1) % klen
    return box


def decode_ncm(src, out_dir):
    with open(src, "rb") as f:
        if f.read(8) != b"CTENFDAM":
            raise RuntimeError("不是有效的 NCM 文件")
        f.seek(2, 1)
        # RC4 密钥
        key_len = struct.unpack("<I", f.read(4))[0]
        kd = bytearray(f.read(key_len))
        for i in range(len(kd)):
            kd[i] ^= 0x64
        dec = _pkcs7_unpad(AES.new(NCM_CORE_KEY, AES.MODE_ECB).decrypt(bytes(kd)))
        key = dec[17:]   # 去掉前缀 'neteasecloudmusic'
        box = _ncm_key_box(key)
        # 元数据
        meta_len = struct.unpack("<I", f.read(4))[0]
        meta, fmt = {}, None
        if meta_len:
            md = bytearray(f.read(meta_len))
            for i in range(len(md)):
                md[i] ^= 0x63
            try:
                b64 = bytes(md)[22:]   # 去掉 '163 key(Don't modify):'
                mdec = _pkcs7_unpad(AES.new(NCM_META_KEY, AES.MODE_ECB).decrypt(base64.b64decode(b64)))
                meta = json.loads(mdec[6:])   # 去掉 'music:'
                fmt = meta.get("format")
            except Exception:
                pass
        f.read(4)            # crc32
        f.seek(5, 1)         # gap
        img_len = struct.unpack("<I", f.read(4))[0]
        cover = f.read(img_len) if img_len else b""
        # 音频数据流解密
        audio = bytearray(f.read())
        for i in range(len(audio)):
            j = (i + 1) & 0xff
            audio[i] ^= box[(box[j] + box[(box[j] + j) & 0xff]) & 0xff]
    if fmt not in ("flac", "mp3"):
        fmt = "flac" if bytes(audio[:4]) == b"fLaC" else "mp3"
    base = os.path.splitext(os.path.basename(src))[0]
    out = os.path.join(out_dir, base + "." + fmt)
    with open(out, "wb") as w:
        w.write(audio)
    # 尽量写入标题/歌手/专辑 + 封面（失败不影响）
    try:
        title = meta.get("musicName", base)
        art = meta.get("artist")
        if isinstance(art, list):
            artists = " / ".join(str(a[0]) for a in art if isinstance(a, list) and a)
        else:
            artists = str(art or "")
        _tag_with_cover(out, fmt, title, artists, meta.get("album", ""), cover)
    except Exception as e:
        print(f"!! NCM 标签写入失败（不影响文件）: {e}")
    return out, fmt


def _tag_with_cover(path, ext, title, artists, album, cover):
    if ext == "flac":
        from mutagen.flac import FLAC, Picture
        af = FLAC(path)
        if title:
            af["title"] = title
        if artists:
            af["artist"] = artists
        if album:
            af["album"] = album
        if cover:
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.data = cover
            af.add_picture(pic)
        af.save()
    elif ext == "mp3":
        from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, ID3NoHeaderError
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        if title:
            tags.add(TIT2(encoding=3, text=title))
        if artists:
            tags.add(TPE1(encoding=3, text=artists))
        if album:
            tags.add(TALB(encoding=3, text=album))
        if cover:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        tags.save(path)


_ncm_failed = set()


def convert_all_ncm():
    found = 0
    for n in list(os.listdir(DL_DIR)) if os.path.isdir(DL_DIR) else []:
        if not n.lower().endswith(".ncm"):
            continue
        src = os.path.join(DL_DIR, n)
        if src in _ncm_failed or not os.path.isfile(src):
            continue
        found += 1
        tid = "ncm" + uuid.uuid4().hex[:9]
        with tasks_lock:
            tasks[tid] = {"id": tid, "name": n, "status": "running", "progress": 0,
                          "message": "NCM 解密中...", "file": "", "ts": time.time()}
        try:
            out, fmt = decode_ncm(src, DL_DIR)
            os.remove(src)   # 转换成功后删除原 .ncm
            set_task(tid, status="done", progress=100,
                     message=f"已转换为 {fmt.upper()}：{os.path.basename(out)}")
        except Exception as e:
            _ncm_failed.add(src)
            set_task(tid, status="error", message=f"NCM 转换失败：{e}")
    return found


def ncm_scan_loop():
    while True:
        try:
            convert_all_ncm()
        except Exception:
            pass
        time.sleep(20)


@app.post("/api/ncm/scan")
def api_ncm_scan():
    n = convert_all_ncm()
    return jsonify({"ok": True, "found": n,
                    "message": f"发现并处理 {n} 个 .ncm 文件" if n else "downloads 里没有 .ncm 文件"})


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(DL_DIR, exist_ok=True)
    threading.Thread(target=ncm_scan_loop, daemon=True).start()   # 自动转换 .ncm
    print("==> 媒体下载器已启动（网易云音乐 + 视频/音频）: http://<主机IP>:8083/")
    print(f"==> 访问密码：{'默认 123456（建议在界面修改）' if get_password()==DEFAULT_PASSWORD else '已自定义'}")
    app.run(host="0.0.0.0", port=8083, threaded=True)
