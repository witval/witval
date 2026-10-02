"""番茄小说下载器（网页版，群晖 Docker）

只走番茄小说官方网页接口（fanqienovel.com），不依赖第三方中转服务器：
  - 搜索：/api/author/search/search_book/v1（失败回退 /api/novel/channel/homepage/search/search/v1/）
  - 详情：/page/<book_id> 页面里的 window.__INITIAL_STATE__
  - 目录：/api/reader/directory/detail?bookId=<book_id>
  - 正文：/reader/<item_id> 页面里的 window.__INITIAL_STATE__

网页正文用自定义字体做了混淆（常用字被替换成私用区 U+E000~U+F8FF 字符）。
这里下载该字体，把每个混淆字符画成图片再用 Tesseract OCR 认出真字，结果按字体缓存到
/data/fontmaps/，番茄换字体也会自动重新识别。认错的字可在 /data/charmap_override.json 手动修正。
"""
import hashlib
import io
import json
import os
import re
import threading
import time
import uuid
import html as htmllib
from concurrent.futures import ThreadPoolExecutor

# tesseract 默认每个进程开满 CPU 线程，多个进程并行时会互相抢 CPU，慢几百倍
os.environ.setdefault("OMP_THREAD_LIMIT", "1")

import requests
from flask import Flask, jsonify, request, send_file, abort

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
PORT = int(os.environ.get("APP_PORT", "8084"))
FANQIE_BASE = os.environ.get("FANQIE_BASE", "https://fanqienovel.com").rstrip("/")

CACHE_DIR = os.path.join(DATA_DIR, "cache")          # 已下载章节缓存（断点续传 / 重试）
FONTMAP_DIR = os.path.join(DATA_DIR, "fontmaps")     # 字体识别结果缓存
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
OVERRIDE_FILE = os.path.join(DATA_DIR, "charmap_override.json")
for _d in (DATA_DIR, DOWNLOAD_DIR, CACHE_DIR, FONTMAP_DIR):
    os.makedirs(_d, exist_ok=True)

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------- 设置 ----------------

DEFAULT_SETTINGS = {
    "cookie": "",          # 可选：浏览器登录 fanqienovel.com 后复制的 Cookie
    "concurrency": 2,      # 同时下载章节数（太大容易被风控）
    "delay": 0.8,          # 每章请求间隔秒数
    "format": "txt",
    "filename": "{title} - {author}",
}
settings_lock = threading.Lock()


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return {**DEFAULT_SETTINGS, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def save_settings(s):
    with settings_lock:
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)


# ---------------- 访问密码（与媒体下载器一致） ----------------

AUTH_SALT = "fanqie-web-auth-v1"
PASSWORD_FILE = os.path.join(DATA_DIR, "app_password.txt")
DEFAULT_PASSWORD = os.environ.get("APP_PASSWORD", "123456")


def get_password():
    try:
        with open(PASSWORD_FILE) as f:
            return f.read().strip() or DEFAULT_PASSWORD
    except OSError:
        return DEFAULT_PASSWORD


def set_password(pw):
    with open(PASSWORD_FILE, "w") as f:
        f.write(pw)


def auth_token():
    return hashlib.sha256((AUTH_SALT + get_password()).encode()).hexdigest()


def is_authed():
    return request.cookies.get("auth") == auth_token()


AUTH_EXEMPT = {"/api/auth/login", "/api/auth/status"}


@app.before_request
def _auth_guard():
    p = request.path
    if not p.startswith("/api/") or p in AUTH_EXEMPT:
        return
    if not is_authed():
        return jsonify({"error": "未登录或密码已更改，请重新登录", "need_auth": True}), 401


# ---------------- 番茄网页请求 ----------------

_tls = threading.local()


def http():
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": FANQIE_BASE + "/",
        })
        _tls.s = s
    cookie = load_settings().get("cookie", "").strip()
    if cookie:
        s.headers["Cookie"] = cookie
    else:
        s.headers.pop("Cookie", None)
    return s


class FanqieError(Exception):
    pass


def fq_get(path, params=None, *, as_json=True, retries=3, timeout=20):
    url = path if path.startswith("http") else FANQIE_BASE + path
    last = None
    for i in range(retries):
        try:
            r = http().get(url, params=params, timeout=timeout)
            if r.status_code in (403, 429):
                last = FanqieError(f"番茄返回 {r.status_code}（可能被风控，稍后再试或调低并发）")
                time.sleep(2 + i * 3)
                continue
            r.raise_for_status()
            if not as_json:
                r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "utf-8"
                return r.text
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(1 + i * 2)
    raise FanqieError(f"请求失败：{url} → {last}")


def extract_initial_state(page):
    """从页面 HTML 中取出 window.__INITIAL_STATE__ 的 JSON（把 undefined 换成 null）。"""
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*", page)
    if not m:
        return None
    i = page.find("{", m.end())
    if i < 0:
        return None
    out, depth, in_str, esc, j = [], 0, None, False, i
    while j < len(page):
        c = page[j]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == in_str:
                in_str = None
        elif c in "\"'":
            in_str = c
            out.append(c)
        elif c == "{" or c == "[":
            depth += 1
            out.append(c)
        elif c == "}" or c == "]":
            depth -= 1
            out.append(c)
            if depth == 0:
                break
        elif page.startswith("undefined", j) and not (out and (out[-1].isalnum() or out[-1] == "_")):
            out.append("null")
            j += len("undefined")
            continue
        else:
            out.append(c)
        j += 1
    try:
        return json.loads("".join(out))
    except ValueError as e:
        log("解析 __INITIAL_STATE__ 失败：", e)
        return None


def dig(obj, *keys, default=None):
    """依次尝试多个键名（兼容驼峰/下划线），返回第一个非空值。"""
    if not isinstance(obj, dict):
        return default
    for k in keys:
        v = obj.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def find_dicts_with(obj, key, limit=2000):
    """递归找出所有包含 key 的字典（接口字段经常变，不写死层级）。"""
    found, stack = [], [obj]
    while stack and len(found) < limit:
        o = stack.pop()
        if isinstance(o, dict):
            if key in o:
                found.append(o)
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(reversed(o))
    return found


STATUS = {"0": "已完结", "1": "连载中", "2": "已完结"}


def norm_book(d):
    bid = str(dig(d, "book_id", "bookId", "id", default=""))
    status = str(dig(d, "creation_status", "creationStatus", default=""))
    return {
        "book_id": bid,
        "title": htmllib.unescape(str(dig(d, "book_name", "bookName", "title", default=""))),
        "author": htmllib.unescape(str(dig(d, "author", "author_name", "authorName", default=""))),
        "cover": dig(d, "thumb_url", "thumbUrl", "thumb_uri", "thumbUri", "cover", default=""),
        "abstract": htmllib.unescape(str(dig(d, "abstract", "book_abstract", "bookAbstract", "description", default=""))),
        "category": str(dig(d, "category", "categoryV2", "category_name", default="")),
        "word_count": dig(d, "word_count", "wordNumber", "word_number", "wordCount", default=0),
        "status": STATUS.get(status, status),
        "chapter_count": dig(d, "serial_count", "chapterTotal", "chapter_total", default=None),
    }


BOOK_ID_RE = re.compile(r"(?:page/|book_id=|bookId=|/reader/)?(\d{12,22})")


def parse_book_id(text):
    text = (text or "").strip()
    if not text:
        return None
    if "/reader/" in text:
        return None  # 章节链接需要先查书
    m = BOOK_ID_RE.search(text)
    if m and (text.isdigit() or "fanqie" in text or "page/" in text or "book_id" in text.lower()):
        return m.group(1)
    return None


def search_books(q, page=0):
    errors = []
    try:
        data = fq_get("/api/author/search/search_book/v1", {
            "filter": "127,127,127,127", "page_count": 10, "page_index": page,
            "query_type": 0, "query_word": q,
        })
        items = find_dicts_with(data.get("data") or data, "book_id")
        books = [norm_book(x) for x in items if dig(x, "book_name", "title")]
        if books:
            return books
    except FanqieError as e:
        errors.append(str(e))
    try:
        data = fq_get("/api/novel/channel/homepage/search/search/v1/", {
            "aid": 1967, "offset": page * 10, "q": q,
        })
        items = find_dicts_with(data.get("data") or data, "book_id")
        books = [norm_book(x) for x in items if dig(x, "book_name", "title")]
        if books:
            return books
    except FanqieError as e:
        errors.append(str(e))
    if errors and page == 0:
        raise FanqieError("；".join(errors))
    return []


def get_directory(book_id):
    data = fq_get("/api/reader/directory/detail", {"bookId": book_id})
    d = data.get("data") or {}
    chapters = []
    vols = d.get("chapterListWithVolume")
    if isinstance(vols, list) and vols:
        for vol in vols:
            for c in (vol if isinstance(vol, list) else [vol]):
                if not isinstance(c, dict):
                    continue
                iid = str(dig(c, "itemId", "item_id", default=""))
                if iid:
                    chapters.append({
                        "item_id": iid,
                        "title": dig(c, "title", default=""),
                        "volume": dig(c, "volume_name", "volumeName", default=""),
                        "locked": bool(dig(c, "isChapterLock", "is_chapter_lock", default=False)),
                    })
    if not chapters:
        for iid in d.get("allItemIds") or []:
            chapters.append({"item_id": str(iid), "title": "", "volume": "", "locked": False})
    if not chapters:
        raise FanqieError("没有拿到章节目录（书籍 ID 不对，或接口变了）")
    return chapters


def get_book(book_id):
    info = {"book_id": book_id}
    try:
        page = fq_get(f"/page/{book_id}", as_json=False)
        state = extract_initial_state(page) or {}
        p = state.get("page") or {}
        if not p:
            cands = find_dicts_with(state, "bookName")
            p = cands[0] if cands else {}
        info.update({k: v for k, v in norm_book({**p, "bookId": book_id}).items() if v not in ("", None, 0)})
        if not info.get("title"):
            m = re.search(r"<title>([^<_|-]+)", page)
            if m:
                info["title"] = m.group(1).strip()
    except FanqieError as e:
        log("详情页获取失败：", e)
    info["chapters"] = get_directory(book_id)
    info.setdefault("title", f"番茄小说{book_id}")
    info.setdefault("author", "")
    info["chapter_count"] = len(info["chapters"])
    return info


# ---------------- 字体反混淆（OCR） ----------------

PUA = re.compile("[-]")
font_lock = threading.Lock()
font_maps = {}            # font_url -> {codepoint_char: real_char}
FONT_URL_RE = re.compile(r"url\(\s*['\"]?(https?:[^'\")]+?\.(?:woff2?|ttf|otf))(?:\?[^'\")]*)?['\"]?\s*\)", re.I)


def load_override():
    try:
        with open(OVERRIDE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        out = {}
        for k, v in raw.items():
            if k.isdigit():
                out[chr(int(k))] = v
            elif k.lower().startswith(("u+", "\\u")):
                out[chr(int(k[2:], 16))] = v
            else:
                out[k] = v
        return out
    except (OSError, ValueError):
        return {}


# 整页 OCR 的几种画法：(前缀参考字, 后缀参考字, 字号)。按实测准确率从高到低排，票数打平时听排前面的。
OCR_VARIANTS = [("我", "的", 64), ("H", "", 48), ("H", "", 64), ("是", "。", 56),
                ("", "", 40), ("H", "", 80), ("一", "", 64), ("", "", 64)]


def _ocr_variant(font_path, cps, pre, post, size, per_page=40):
    """一张图画 40 行，每行「参考字 + 混淆字 + 参考字」，整页识别一次再按行对回去。
    逐字单独识别既慢（每次都要加载中文模型）又不准（tesseract 需要一行字的上下文）。"""
    from PIL import Image, ImageDraw, ImageFont
    import pytesseract

    gfont = ImageFont.truetype(font_path, size)
    ref = ImageFont.truetype(REF_FONT, size) if REF_FONT else gfont
    lh = int(size * 1.8)

    def page(chunk):
        im = Image.new("L", (size * 7, lh * len(chunk) + size), 255)
        dr = ImageDraw.Draw(im)
        for i, cp in enumerate(chunk):
            x, y = size // 2, size // 2 + i * lh
            if pre:
                dr.text((x, y), pre, font=ref, fill=0)
                x += int(ref.getlength(pre) + size * 0.15)
            dr.text((x, y), chr(cp), font=gfont, fill=0)
            x += int(size * 1.15)
            if post:
                dr.text((x, y), post, font=ref, fill=0)
        d = pytesseract.image_to_data(im, lang="chi_sim", config="--psm 6",
                                      output_type=pytesseract.Output.DICT)
        lines = {}
        for txt, left, top, h in zip(d["text"], d["left"], d["top"], d["height"]):
            txt = re.sub(r"\s+", "", txt or "")
            if not txt:
                continue
            idx = int((top + h / 2 - size // 2) // lh)
            if 0 <= idx < len(chunk):
                lines.setdefault(idx, []).append((left, txt))
        out = {}
        for idx, parts in lines.items():
            t = "".join(x for _, x in sorted(parts))
            if pre:
                if not t.startswith(pre):
                    continue
                t = t[len(pre):]
            if post:
                if not t.endswith(post):
                    continue
                t = t[:-len(post)]
            if len(t) == 1:
                out[chunk[idx]] = t
        return out

    res = {}
    for i in range(0, len(cps), per_page):
        res.update(page(cps[i:i + per_page]))
    return res


def _glyph_metrics(font_path, cps):
    """每个字形的 (yMin, yMax)，以及字体的小写字母高度/大写字母高度，用于判断英文大小写。"""
    from fontTools.ttLib import TTFont
    from fontTools.pens.boundsPen import BoundsPen
    tt = TTFont(font_path)
    gs, cmap = tt.getGlyphSet(), tt.getBestCmap()
    upm = tt["head"].unitsPerEm
    bounds = {}
    for cp in cps:
        pen = BoundsPen(gs)
        try:
            gs[cmap[cp]].draw(pen)
        except Exception:  # noqa: BLE001
            continue
        if pen.bounds:
            bounds[cp] = (pen.bounds[1] / upm, pen.bounds[3] / upm)
    os2 = tt["OS/2"] if "OS/2" in tt else None
    xh = (getattr(os2, "sxHeight", 0) or 0) / upm if os2 else 0
    cap = (getattr(os2, "sCapHeight", 0) or 0) / upm if os2 else 0
    return bounds, (xh or 0.52), (cap or 0.72)


CASE_PAIRS = set("cosuvwxzp")   # 大小写只差大小的字母


def _fix_latin(ch, b, xh, cap):
    if not b:
        return ch
    ymin, ymax = b
    lower = ymax < (xh + cap) / 2
    desc = ymin < -0.08
    if ch == "|":
        return "l"
    if ch.lower() in CASE_PAIRS and ch.isalpha():
        if ch.lower() == "p":
            return "p" if desc else "P"
        return ch.lower() if lower else ch.upper()
    if desc and ch in "98":
        return {"9": "q", "8": "g"}[ch]
    return ch


def _render_ocr(font_path, cps):
    """用多种画法分别整页识别，再按多数投票决定每个混淆字是什么字。"""
    from collections import Counter

    results = [None] * len(OCR_VARIANTS)
    workers = max(1, min(4, os.cpu_count() or 2))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_ocr_variant, font_path, cps, *v): i for i, v in enumerate(OCR_VARIANTS)}
        for f, i in futs.items():
            try:
                results[i] = f.result()
            except Exception as e:  # noqa: BLE001
                log("OCR 失败：", OCR_VARIANTS[i], e)
                results[i] = {}
    try:
        bounds, xh, cap = _glyph_metrics(font_path, cps)
    except Exception as e:  # noqa: BLE001
        log("读取字形尺寸失败：", e)
        bounds, xh, cap = {}, 0.52, 0.72
    out = {}
    for cp in cps:
        votes = [r[cp] for r in results if cp in r]
        if not votes:
            continue
        if any(ord(v) < 128 or v == "|" for v in votes):
            votes = [_fix_latin(v, bounds.get(cp), xh, cap) if (ord(v) < 128 or v == "|") else v for v in votes]
        cnt = Counter(votes)
        top = max(cnt.values())
        out[cp] = next(v for v in votes if cnt[v] == top)   # 平票时取排在前面（更准）的画法
    return out


def _find_ref_font():
    for p in ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            return p
    return None


REF_FONT = _find_ref_font()


def get_font_map(font_url):
    with font_lock:
        if font_url in font_maps:
            return font_maps[font_url]
        key = hashlib.md5(font_url.encode()).hexdigest()
        cache = os.path.join(FONTMAP_DIR, key + ".json")
        if os.path.exists(cache):
            with open(cache, encoding="utf-8") as f:
                m = {chr(int(k)): v for k, v in json.load(f)["map"].items()}
            font_maps[font_url] = m
            return m
        from fontTools.ttLib import TTFont
        log("发现新混淆字体，开始识别（首次约 1~3 分钟）：", font_url)
        raw = http().get(font_url, timeout=30).content
        tt = TTFont(io.BytesIO(raw))
        cps = sorted(cp for cp in tt.getBestCmap() if 0xE000 <= cp <= 0xF8FF)
        tt.flavor = None
        ttf_path = os.path.join(FONTMAP_DIR, key + ".ttf")
        tt.save(ttf_path)
        t0 = time.time()
        res = _render_ocr(ttf_path, cps)
        m = {chr(cp): v for cp, v in res.items() if v}
        miss = len(cps) - len(m)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({"url": font_url, "map": {str(ord(k)): v for k, v in m.items()},
                       "total": len(cps), "created": time.time()}, f, ensure_ascii=False, indent=0)
        log(f"字体识别完成：{len(m)}/{len(cps)} 个字，用时 {time.time() - t0:.0f}s" + (f"，{miss} 个未识别" if miss else ""))
        font_maps[font_url] = m
        return m


def decode_text(text, font_urls):
    if not PUA.search(text):
        return text
    maps = []
    for u in font_urls:
        try:
            maps.append(get_font_map(u))
        except Exception as e:  # noqa: BLE001
            log("字体获取/识别失败：", u, e)
    used = set(PUA.findall(text))
    # 选覆盖混淆字最多的那套字体（正文和标题可能用不同字体）
    maps.sort(key=lambda m: -len(used & m.keys()))
    override = load_override()

    def rep(mo):
        ch = mo.group(0)
        if ch in override:
            return override[ch]
        for m in maps:
            if ch in m:
                return m[ch]
        return ch
    return PUA.sub(rep, text)


# ---------------- 章节正文 ----------------

def html_to_paragraphs(content):
    content = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", content)
    content = re.sub(r"(?i)<br\s*/?>", "\n", content)
    content = re.sub(r"(?i)</p\s*>", "\n", content)
    content = re.sub(r"(?s)<[^>]+>", "", content)
    content = htmllib.unescape(content).replace("　", " ")
    return [ln.strip() for ln in content.split("\n") if ln.strip()]


LOCK_HINTS = ("登录后", "继续阅读", "开通会员", "下载番茄小说")


def get_chapter(item_id):
    page = fq_get(f"/reader/{item_id}", as_json=False)
    fonts = list(dict.fromkeys(FONT_URL_RE.findall(page)))
    state = extract_initial_state(page) or {}
    cd = (state.get("reader") or {}).get("chapterData") or {}
    if not cd:
        cands = find_dicts_with(state, "content")
        cd = next((c for c in cands if isinstance(c.get("content"), str) and len(c["content"]) > 50), {})
    content = cd.get("content") or ""
    title = cd.get("title") or ""
    if not content:
        m = re.search(r'(?is)<div[^>]+class="[^"]*muye-reader-content[^"]*"[^>]*>(.*?)</div>\s*</div>', page)
        content = m.group(1) if m else ""
        t = re.search(r'(?is)<h1[^>]*class="[^"]*muye-reader-title[^"]*"[^>]*>(.*?)</h1>', page)
        title = title or (html_to_paragraphs(t.group(1))[0] if t else "")
    if not content:
        raise FanqieError("页面里没有正文（可能需要登录 Cookie，或被风控）")
    paras = html_to_paragraphs(decode_text(content, fonts))
    title = decode_text(title, fonts)
    partial = len(paras) <= 3 and any(h in "".join(paras) for h in LOCK_HINTS)
    return {"title": title, "paragraphs": paras, "partial": partial,
            "word_count": sum(len(p) for p in paras)}


def cached_chapter(book_id, item_id, force=False):
    d = os.path.join(CACHE_DIR, book_id)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, item_id + ".json")
    if not force and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    ch = get_chapter(item_id)
    if ch["partial"]:
        raise FanqieError("只拿到试读内容（需要在设置里填登录 Cookie）")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ch, f, ensure_ascii=False)
    return ch


# ---------------- 导出 ----------------

def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", s).strip(" .")
    return s[:120] or "未命名"


def out_filename(book, fmt):
    pat = load_settings().get("filename") or DEFAULT_SETTINGS["filename"]
    try:
        name = pat.format(title=book.get("title", ""), author=book.get("author", ""), book_id=book["book_id"])
    except (KeyError, IndexError, ValueError):
        name = f"{book.get('title', '')} - {book.get('author', '')}"
    return safe_name(name.strip(" -")) + "." + fmt


def export_txt(book, chapters, path):
    with open(path + ".part", "w", encoding="utf-8") as f:
        f.write(f"{book.get('title', '')}\n作者：{book.get('author', '')}\n")
        if book.get("abstract"):
            f.write(f"\n简介：\n{book['abstract']}\n")
        f.write(f"\n来源：{FANQIE_BASE}/page/{book['book_id']}\n")
        for meta, ch in chapters:
            title = ch.get("title") or meta.get("title") or ""
            f.write(f"\n\n{title}\n\n")
            f.write("\n".join("　　" + p for p in ch["paragraphs"]))
            f.write("\n")
    os.replace(path + ".part", path)


def export_epub(book, chapters, path):
    from ebooklib import epub
    eb = epub.EpubBook()
    eb.set_identifier(f"fanqie-{book['book_id']}")
    eb.set_title(book.get("title", ""))
    eb.set_language("zh")
    if book.get("author"):
        eb.add_author(book["author"])
    if book.get("cover"):
        try:
            r = http().get(book["cover"], timeout=20)
            if r.ok and r.content:
                eb.set_cover("cover.jpg", r.content)
        except requests.RequestException:
            pass
    css = epub.EpubItem(uid="css", file_name="style.css", media_type="text/css",
                        content=b"p{text-indent:2em;margin:.4em 0;line-height:1.7}h2{text-align:center}")
    eb.add_item(css)
    intro = epub.EpubHtml(title="简介", file_name="intro.xhtml", lang="zh")
    intro.content = (f"<h1>{htmllib.escape(book.get('title', ''))}</h1><p>作者：{htmllib.escape(book.get('author', ''))}</p>"
                     + "".join(f"<p>{htmllib.escape(p)}</p>" for p in html_to_paragraphs(book.get("abstract", ""))))
    intro.add_item(css)
    eb.add_item(intro)
    items = [intro]
    for i, (meta, ch) in enumerate(chapters, 1):
        title = ch.get("title") or meta.get("title") or f"第{i}章"
        c = epub.EpubHtml(title=title, file_name=f"c{i:05d}.xhtml", lang="zh")
        c.content = f"<h2>{htmllib.escape(title)}</h2>" + "".join(f"<p>{htmllib.escape(p)}</p>" for p in ch["paragraphs"])
        c.add_item(css)
        eb.add_item(c)
        items.append(c)
    eb.toc = items
    eb.spine = ["nav"] + items
    eb.add_item(epub.EpubNcx())
    eb.add_item(epub.EpubNav())
    epub.write_epub(path + ".part", eb)
    os.replace(path + ".part", path)


# ---------------- 下载任务 ----------------

tasks = {}
tasks_lock = threading.Lock()
task_queue_sem = threading.Semaphore(1)   # 一次只跑一本书，避免被风控


def save_tasks():
    with tasks_lock:
        data = [{k: v for k, v in t.items() if not k.startswith("_")} for t in tasks.values()]
    tmp = TASKS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, TASKS_FILE)


def load_tasks():
    try:
        with open(TASKS_FILE, encoding="utf-8") as f:
            for t in json.load(f):
                if t.get("status") in ("排队中", "下载中", "导出中"):
                    t["status"] = "已中断"
                    t["message"] = "容器重启中断，点「重试」继续（已下载章节不会重下）"
                tasks[t["id"]] = t
    except (OSError, ValueError):
        pass


def update_task(t, **kw):
    with tasks_lock:
        t.update(kw)


def run_task(t):
    with task_queue_sem:
        if t.get("_cancel"):
            update_task(t, status="已取消", message="")
            save_tasks()
            return
        try:
            _run_task(t)
        except Exception as e:  # noqa: BLE001
            log("任务失败：", t["title"], e)
            update_task(t, status="失败", message=str(e))
        save_tasks()


def _run_task(t):
    update_task(t, status="下载中", message="获取目录…")
    book = get_book(t["book_id"])
    chs = book["chapters"]
    start = max(1, int(t.get("start") or 1))
    end = min(len(chs), int(t.get("end") or len(chs)))
    sel = chs[start - 1:end]
    update_task(t, title=book.get("title") or t["title"], author=book.get("author", ""),
                total=len(sel), done=0, failed=[], message="")
    st = load_settings()
    conc = max(1, min(8, int(st.get("concurrency") or 2)))
    delay = max(0.0, float(st.get("delay") or 0))
    results = [None] * len(sel)
    failed = []

    def work(i):
        if t.get("_cancel"):
            return
        meta = sel[i]
        try:
            hit = os.path.exists(os.path.join(CACHE_DIR, t["book_id"], meta["item_id"] + ".json"))
            results[i] = cached_chapter(t["book_id"], meta["item_id"])
            if not hit and delay:
                time.sleep(delay)
        except Exception as e:  # noqa: BLE001
            failed.append({"index": start + i, "title": meta.get("title", ""), "error": str(e)})
        with tasks_lock:
            t["done"] = t.get("done", 0) + 1
            t["failed"] = sorted(failed, key=lambda x: x["index"])

    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(work, range(len(sel))))
    if t.get("_cancel"):
        update_task(t, status="已取消", message="已下载的章节已缓存，重新下载会跳过")
        return
    good = [(sel[i], r) for i, r in enumerate(results) if r]
    if not good:
        raise FanqieError(failed[0]["error"] if failed else "一章都没下载成功")
    update_task(t, status="导出中", message="")
    fname = out_filename(book, t["format"])
    path = os.path.join(DOWNLOAD_DIR, fname)
    (export_epub if t["format"] == "epub" else export_txt)(book, good, path)
    msg = f"共 {len(good)} 章"
    if failed:
        msg += f"，{len(failed)} 章失败（已跳过，可点「重试」补全）"
    update_task(t, status="完成" if not failed else "部分完成", file=fname, message=msg, finished=time.time())
    log("完成：", fname, msg)


def start_task(t):
    t["_cancel"] = False
    threading.Thread(target=run_task, args=(t,), daemon=True).start()


# ---------------- 路由 ----------------

@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/auth/status")
def auth_status():
    return jsonify({"authed": is_authed(), "is_default": get_password() == DEFAULT_PASSWORD})


@app.post("/api/auth/login")
def auth_login():
    data = request.get_json(silent=True) or {}
    if data.get("password") == get_password():
        resp = jsonify({"ok": True})
        resp.set_cookie("auth", auth_token(), max_age=30 * 86400, httponly=True, samesite="Lax")
        return resp
    return jsonify({"error": "密码错误"}), 403


@app.post("/api/auth/change")
def auth_change():
    data = request.get_json(silent=True) or {}
    if data.get("old") != get_password():
        return jsonify({"error": "当前密码不正确"}), 403
    new = (data.get("new") or "").strip()
    if len(new) < 4:
        return jsonify({"error": "新密码至少 4 位"}), 400
    set_password(new)
    resp = jsonify({"ok": True})
    resp.set_cookie("auth", auth_token(), max_age=30 * 86400, httponly=True, samesite="Lax")
    return resp


@app.post("/api/auth/logout")
def auth_logout():
    resp = jsonify({"ok": True})
    resp.delete_cookie("auth")
    return resp


def err(e, code=502):
    return jsonify({"error": str(e)}), code


@app.get("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    page = int(request.args.get("page") or 0)
    if not q:
        return err("请输入关键词", 400)
    bid = parse_book_id(q)
    if bid:
        return jsonify({"direct": bid, "books": []})
    try:
        return jsonify({"books": search_books(q, page)})
    except FanqieError as e:
        return err(e)


@app.get("/api/book/<book_id>")
def api_book(book_id):
    if not book_id.isdigit():
        return err("书籍 ID 不对", 400)
    try:
        b = get_book(book_id)
    except FanqieError as e:
        return err(e)
    cdir = os.path.join(CACHE_DIR, book_id)
    cached = set(x[:-5] for x in os.listdir(cdir)) if os.path.isdir(cdir) else set()
    for c in b["chapters"]:
        c["cached"] = c["item_id"] in cached
    return jsonify(b)


@app.get("/api/chapter/<item_id>")
def api_chapter(item_id):
    """在线预览 / 测试反混淆效果。"""
    if not item_id.isdigit():
        return err("章节 ID 不对", 400)
    try:
        return jsonify(get_chapter(item_id))
    except FanqieError as e:
        return err(e)


@app.post("/api/download")
def api_download():
    d = request.get_json(silent=True) or {}
    bid = str(d.get("book_id") or "")
    if not bid.isdigit():
        return err("书籍 ID 不对", 400)
    fmt = d.get("format") if d.get("format") in ("txt", "epub") else load_settings().get("format", "txt")
    t = {"id": uuid.uuid4().hex[:10], "book_id": bid, "title": d.get("title") or bid,
         "author": d.get("author") or "", "format": fmt,
         "start": d.get("start") or 1, "end": d.get("end") or 0,
         "status": "排队中", "done": 0, "total": 0, "failed": [], "message": "",
         "file": "", "created": time.time()}
    with tasks_lock:
        tasks[t["id"]] = t
    start_task(t)
    save_tasks()
    return jsonify({"ok": True, "id": t["id"]})


@app.get("/api/tasks")
def api_tasks():
    with tasks_lock:
        data = [{k: v for k, v in t.items() if not k.startswith("_")} for t in tasks.values()]
    data.sort(key=lambda x: -x.get("created", 0))
    return jsonify({"tasks": data})


@app.post("/api/tasks/<tid>/<action>")
def api_task_action(tid, action):
    t = tasks.get(tid)
    if not t:
        return err("任务不存在", 404)
    if action == "cancel":
        t["_cancel"] = True
        if t["status"] == "排队中":
            update_task(t, status="已取消")
    elif action == "retry":
        if t["status"] in ("排队中", "下载中", "导出中"):
            return err("任务还在进行中", 400)
        update_task(t, status="排队中", message="", done=0)
        start_task(t)
    elif action == "delete":
        if t["status"] in ("下载中", "导出中"):
            t["_cancel"] = True
        with tasks_lock:
            tasks.pop(tid, None)
    else:
        return err("未知操作", 400)
    save_tasks()
    return jsonify({"ok": True})


@app.get("/api/settings")
def api_settings_get():
    s = load_settings()
    s["cookie_set"] = bool(s.pop("cookie", ""))
    s["fonts"] = len([x for x in os.listdir(FONTMAP_DIR) if x.endswith(".json")])
    return jsonify(s)


@app.post("/api/settings")
def api_settings_set():
    d = request.get_json(silent=True) or {}
    s = load_settings()
    if "cookie" in d:
        s["cookie"] = str(d["cookie"]).strip().replace("\n", "")
    for k, typ in (("concurrency", int), ("delay", float)):
        if k in d:
            try:
                s[k] = typ(d[k])
            except (TypeError, ValueError):
                return err(f"{k} 格式不对", 400)
    s["concurrency"] = max(1, min(8, s["concurrency"]))
    s["delay"] = max(0.0, min(10.0, s["delay"]))
    if d.get("format") in ("txt", "epub"):
        s["format"] = d["format"]
    if "filename" in d and str(d["filename"]).strip():
        s["filename"] = str(d["filename"]).strip()
    save_settings(s)
    return jsonify({"ok": True})


@app.post("/api/fontmaps/clear")
def api_fontmaps_clear():
    with font_lock:
        font_maps.clear()
        for x in os.listdir(FONTMAP_DIR):
            os.remove(os.path.join(FONTMAP_DIR, x))
    return jsonify({"ok": True})


@app.get("/api/files")
def api_files():
    out = []
    for x in os.listdir(DOWNLOAD_DIR):
        p = os.path.join(DOWNLOAD_DIR, x)
        if os.path.isfile(p) and x.lower().endswith((".txt", ".epub")):
            st = os.stat(p)
            out.append({"name": x, "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda x: -x["mtime"])
    return jsonify({"files": out})


@app.get("/api/files/<path:name>")
def api_file(name):
    p = os.path.realpath(os.path.join(DOWNLOAD_DIR, name))
    if not p.startswith(os.path.realpath(DOWNLOAD_DIR) + os.sep) or not os.path.isfile(p):
        abort(404)
    return send_file(p, as_attachment=True, download_name=name)


@app.get("/api/diag")
def api_diag():
    """连通性自检：能否访问番茄、OCR 是否可用。"""
    out = {"fanqie_base": FANQIE_BASE, "cookie": bool(load_settings().get("cookie"))}
    try:
        r = http().get(FANQIE_BASE + "/", timeout=10)
        out["fanqie"] = f"HTTP {r.status_code}"
    except requests.RequestException as e:
        out["fanqie"] = f"连不上：{e.__class__.__name__}"
    try:
        import pytesseract
        out["tesseract"] = str(pytesseract.get_tesseract_version())
        out["ocr_langs"] = [x for x in pytesseract.get_languages(config="") if x != "osd"]
    except Exception as e:  # noqa: BLE001
        out["tesseract"] = f"不可用：{e}"
    return jsonify(out)


load_tasks()

if __name__ == "__main__":
    log("==> 番茄小说下载器（网页版）已启动，端口", PORT)
    log(f"==> 访问密码：{'默认 123456（建议在界面修改）' if get_password() == DEFAULT_PASSWORD else '已自定义'}")
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=16)
