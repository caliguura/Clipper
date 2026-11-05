"""Локальный загрузчик YouTube с выбором таймкодов (глав).
Запуск: python app.py  ->  откроется http://127.0.0.1:5000
Нужны: Python 3.9+, ffmpeg в PATH, pip install -r requirements.txt
"""
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
import webbrowser
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, Response, jsonify, request, send_from_directory
from yt_dlp.utils import download_range_func, sanitize_filename

FROZEN = getattr(sys, "frozen", False)  # запущено как exe (PyInstaller)
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))  # встроенные ресурсы: index.html, icon.ico
APP_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))  # рядом с exe: downloads, cookies.txt, bin
URL = "http://127.0.0.1:5000"

if FROZEN:  # без консоли stdout/stderr = None, поэтому пишем лог в файл
    _log = os.path.join(os.environ.get("LOCALAPPDATA", APP_DIR), "YT Downloader")
    os.makedirs(_log, exist_ok=True)
    sys.stdout = sys.stderr = open(os.path.join(_log, "app.log"), "w", encoding="utf-8", buffering=1)
_bin = os.path.join(APP_DIR, "bin")  # ffmpeg и deno лежат в папке bin рядом с программой
if os.path.isdir(_bin):
    os.environ["PATH"] = _bin + os.pathsep + os.environ.get("PATH", "")
OUT = os.path.join(APP_DIR, "downloads")
os.makedirs(OUT, exist_ok=True)

app = Flask(__name__)
jobs = {}
CACHE = {}      # url -> ссылки на слайды карусели
PREVIEWS = {}   # token -> (прямая ссылка на видео, заголовки)
IMG = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".avif")
AUDIO = ("mp3", "m4a", "wav", "flac", "opus", "orig")  # orig - исходный аудиопоток без конвертации
VIDEO_MID = {"mp4": "mp4", "mkv": "mkv", "webm": "mkv", "mov": "mp4", "gif": "mp4"}  # во что скачиваем до конвертации
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def convert(src, dst, fmt):
    """WebM / MOV / GIF делаем через ffmpeg после скачивания."""
    vf = "fps=15,scale='min(480,iw)':-2:flags=lanczos,split[s0][s1];[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5"
    tries = {
        "gif": [["-an", "-vf", vf, "-loop", "0"]],
        "webm": [["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1", "-deadline", "good", "-cpu-used", "4", "-c:a", "libopus"]],
        "mov": [["-c", "copy"], ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac"]],
    }[fmt]
    err = ""
    for args in tries:
        p = subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-y", "-i", src, *args, dst], capture_output=True, text=True,
                           errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if p.returncode == 0:
            return
        err = p.stderr[-400:]
    raise RuntimeError("ffmpeg: " + err)


def carousel_slides(d):
    """Фото-карусель TikTok (/photo/): yt-dlp отдаёт плейлист из картинок."""
    if not (d.get("extractor_key") or "").lower().startswith("tiktok") or d.get("formats") or not d.get("entries"):
        return []
    out = []
    for e in d["entries"]:
        if not e:
            continue
        u = e.get("url") or ""
        if not urlparse(u).path.lower().endswith(IMG):
            fm = [f for f in e.get("formats") or [] if urlparse(f.get("url") or "").path.lower().endswith(IMG)]
            th = sorted(e.get("thumbnails") or [], key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
            u = fm[-1]["url"] if fm else th[-1]["url"] if th else e.get("thumbnail") or ""
        if u.startswith("http"):
            out.append(u)
    return out


def make_preview(d):
    """YouTube - встроенный плеер; остальное - прогрессивный mp4, который отдаём через /api/preview."""
    if (d.get("extractor_key") or "").lower().startswith("youtube") and d.get("id"):
        return {"type": "yt", "id": d["id"]}
    fm = [f for f in d.get("formats") or [] if f.get("url") and f.get("protocol") in ("https", "http")
          and f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")]
    if not fm and (d.get("extractor_key") or "").lower().startswith("soundcloud"):  # аудиотрек без видео
        fm = [f for f in d.get("formats") or [] if f.get("url") and f.get("protocol") in ("https", "http") and f.get("acodec") not in (None, "none")]
    if not fm:
        return None
    f = min(fm, key=lambda x: abs((x.get("height") or 480) - 480))
    if len(PREVIEWS) > 50:
        PREVIEWS.clear()
    token = uuid.uuid4().hex
    PREVIEWS[token] = (f["url"], f.get("http_headers") or d.get("http_headers") or {})
    return {"type": "file", "src": f"/api/preview/{token}"}


def format_selector(quality):
    if quality == "audio":
        return "ba/b"
    if quality == "best":
        return "bv*+ba/b"
    h = int(quality)
    return f"bv*[height<={h}]+ba/b[height<={h}]"


COOKIES_FILE = os.path.join(APP_DIR, "cookies.txt")

MSG = {
    "ru": {"url": "Вставьте ссылку", "job": "Задача не найдена", "ok": "найден", "no": "НЕ найден", "part": "Часть", "gone": "Данные устарели, найдите ссылку заново", "pl": "Плейлисты и сеты пока не поддерживаются, вставьте ссылку на один трек или видео"},
    "uk": {"url": "Вставте посилання", "job": "Завдання не знайдено", "ok": "знайдено", "no": "НЕ знайдено", "part": "Частина", "gone": "Дані застаріли, знайдіть посилання знову", "pl": "Плейлисти та сети поки не підтримуються, вставте посилання на один трек або відео"},
    "en": {"url": "Paste a link", "job": "Job not found", "ok": "found", "no": "NOT found", "part": "Part", "gone": "Data expired, search the link again", "pl": "Playlists and sets are not supported yet, paste a single track or video link"},
}


def tr(lang, key):
    return MSG.get(lang, MSG["ru"])[key]


def auth_opts(cookies):
    """Куки браузера (для 18+ и закрытых видео) или файл cookies.txt рядом с app.py."""
    if cookies and cookies != "none":
        return {"cookiesfrombrowser": (cookies,)}
    if os.path.exists(COOKIES_FILE):
        return {"cookiefile": COOKIES_FILE}
    return {}


def clean(e, cookies=None, lang="ru"):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    if not cookies or cookies == "none":
        state = tr(lang, "ok" if os.path.exists(COOKIES_FILE) else "no")
        msg += f"  |  cookies.txt {state}: {COOKIES_FILE}"
    return msg


@app.get("/")
def index():
    with open(os.path.join(BASE_DIR, "index.html"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


@app.get("/api/preview/<token>")
def preview_stream(token):
    src = PREVIEWS.get(token)
    if not src:
        return "", 404
    hdr = dict(src[1])
    if request.headers.get("Range"):
        hdr["Range"] = request.headers["Range"]
    try:
        r = urllib.request.urlopen(urllib.request.Request(src[0], headers=hdr), timeout=20)
    except urllib.error.HTTPError as e:
        return "", e.code
    except Exception:  # noqa: BLE001
        return "", 502

    def gen():
        try:
            while chunk := r.read(65536):
                yield chunk
        finally:
            r.close()

    h = {k: r.headers[k] for k in ("Content-Type", "Content-Length", "Content-Range") if r.headers.get(k)}
    h["Accept-Ranges"] = "bytes"
    return Response(gen(), status=r.status, headers=h)


@app.post("/api/info")
def info():
    body = request.json or {}
    url = body.get("url", "").strip()
    lang = body.get("lang")
    if not url:
        return jsonify(error=tr(lang, "url")), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True, **auth_opts(body.get("cookies"))}) as ydl:
            d = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        return jsonify(error=clean(e, body.get("cookies"), lang)), 400
    slides = carousel_slides(d)
    if slides:
        CACHE[url] = slides
        return jsonify(title=d.get("title") or d.get("id") or "slides", thumbnail=slides[0], duration=None,
                       uploader=d.get("uploader"), chapters=[], slides=[{"thumb": u} for u in slides])
    if d.get("entries"):
        return jsonify(error=tr(lang, "pl")), 400
    preview = make_preview(d)
    chapters = [
        {"title": c.get("title") or f"{tr(lang, 'part')} {i + 1}", "start": c["start_time"], "end": c["end_time"]}
        for i, c in enumerate(d.get("chapters") or [])
    ]
    return jsonify(
        title=d.get("title"),
        thumbnail=d.get("thumbnail"),
        duration=d.get("duration"),
        uploader=d.get("uploader"),
        chapters=chapters,
        preview=preview,
    )


def estimate_bytes(info, start, end):
    """Примерный размер отрезка: ffmpeg не отдаёт прогресс, поэтому считаем по размеру файла."""
    dur = info.get("duration") or 0
    if not dur:
        return 0
    stop = min(end, dur) if end is not None else dur
    seconds = max(stop - start, 1)
    total = 0
    for f in info.get("requested_formats") or [info]:
        size = f.get("filesize") or f.get("filesize_approx")
        if size:
            total += size * seconds / dur
        elif f.get("tbr"):
            total += f["tbr"] * 125 * seconds
    return total


def run_job(job, url, title, ranges, quality, cookies=None, lang="ru", fmt="mp4"):
    # ranges пустой -> качаем всё видео целиком
    items = ranges or [{"label": "", "start": None, "end": None}]
    if quality == "audio" and fmt not in AUDIO:
        fmt = "mp3"
    if fmt in AUDIO:
        quality = "audio"
    elif fmt == "gif" and quality in ("best", "1080", "720"):
        quality = "480"
    try:
        for i, it in enumerate(items):
            job["current"] = it["label"] or title
            base = sanitize_filename(f"{title} - {it['label']}" if it["label"] else title)[:120].rstrip(" .")

            state = {"done": 0}  # сколько байт уже скачано завершёнными потоками

            def hook(d, i=i, state=state):
                if d["status"] == "downloading":
                    fmts = (d.get("info_dict") or {}).get("requested_formats") or []
                    total = sum((f.get("filesize") or f.get("filesize_approx") or 0) for f in fmts)
                    done = state["done"]
                    if not total:
                        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                        done = 0
                    frac = (done + d["downloaded_bytes"]) / total if total else 0
                    job["progress"] = max(job["progress"], (i + min(frac, 0.99)) / len(items))
                    job["speed"] = d.get("speed") or 0
                elif d["status"] == "finished":
                    state["done"] += d.get("total_bytes") or d.get("downloaded_bytes") or 0

            opts = {
                "quiet": True,
                "noplaylist": True,
                "format": format_selector(quality),
                "merge_output_format": VIDEO_MID.get(fmt, "mp4"),
                "outtmpl": os.path.join(OUT, base.replace("%", "%%") + ".%(ext)s"),
                "progress_hooks": [hook],
                **auth_opts(cookies),
            }
            if quality == "audio" and fmt != "orig":
                pcs = [{"key": "FFmpegExtractAudio", "preferredcodec": fmt}]
                if fmt in ("mp3", "m4a", "flac", "opus"):  # теги и обложка
                    opts["writethumbnail"] = True
                    pcs += [{"key": "FFmpegMetadata", "add_metadata": True}, {"key": "EmbedThumbnail"}]
                opts["postprocessors"] = pcs
            if it["start"] is not None:
                end = it["end"] if it["end"] is not None else float("inf")
                opts["download_ranges"] = download_range_func(None, [(it["start"], end)])
                opts["force_keyframes_at_cuts"] = True  # точная нарезка

            stop_watch = threading.Event()
            with yt_dlp.YoutubeDL(opts) as ydl:
                if it["start"] is not None:
                    est = 0
                    try:
                        est = estimate_bytes(ydl.extract_info(url, download=False), it["start"], it["end"])
                    except Exception:  # noqa: BLE001
                        pass

                    def watch(i=i, est=est, base=base):
                        last_size, last_t, speed = 0, time.time(), 0
                        while not stop_watch.wait(0.5):
                            size = 0
                            for f in os.listdir(OUT):
                                if f.startswith(base + "."):
                                    try:
                                        size += os.path.getsize(os.path.join(OUT, f))
                                    except OSError:
                                        pass
                            now = time.time()
                            if now > last_t and size >= last_size:
                                inst = (size - last_size) / (now - last_t)
                                speed = inst if not speed else speed * 0.7 + inst * 0.3
                                job["speed"] = speed
                            last_size, last_t = size, now
                            if est:
                                job["progress"] = max(job["progress"], (i + min(size / est, 0.98)) / len(items))

                    threading.Thread(target=watch, daemon=True).start()
                try:
                    ydl.download([url])
                finally:
                    stop_watch.set()

            found = [f for f in os.listdir(OUT) if f.startswith(base + ".") and not f.endswith((".part", ".jpg", ".jpeg", ".png", ".webp"))]
            if found:
                name = max(found, key=lambda f: os.path.getmtime(os.path.join(OUT, f)))
                if fmt in ("gif", "webm", "mov") and quality != "audio" and name != f"{base}.{fmt}":
                    job["current"] = f"{it['label'] or title} → {fmt.upper()}"
                    convert(os.path.join(OUT, name), os.path.join(OUT, f"{base}.{fmt}"), fmt)
                    os.remove(os.path.join(OUT, name))
                    name = f"{base}.{fmt}"
                job["files"].append(name)
            job["progress"] = max(job["progress"], (i + 1) / len(items))
            job["speed"] = 0
        job["status"] = "done"
        job["progress"] = 1
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = clean(e, cookies, lang)


def run_carousel(job, url, title, picks, lang=None):
    slides = CACHE.get(url)
    try:
        if not slides:
            raise RuntimeError(tr(lang, "gone"))
        for k, i in enumerate(picks):
            job["current"] = f"{i + 1} / {len(slides)}"
            req = urllib.request.Request(slides[i], headers={"User-Agent": UA, "Referer": "https://www.tiktok.com/"})
            with urllib.request.urlopen(req, timeout=30) as r:
                ext = r.headers.get_content_subtype() if r.headers.get_content_maintype() == "image" else "jpg"
                ext = {"jpeg": "jpg"}.get(ext, ext)
                name = sanitize_filename(f"{title} - {i + 1:02d}")[:120].rstrip(" .") + "." + ext
                with open(os.path.join(OUT, name), "wb") as f:
                    shutil.copyfileobj(r, f)
            job["files"].append(name)
            job["progress"] = (k + 1) / len(picks)
        job["status"] = "done"
        job["progress"] = 1
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = clean(e, None, lang)


@app.post("/api/download")
def download():
    d = request.json or {}
    job = {"status": "running", "progress": 0, "current": "", "files": [], "error": None, "speed": 0, "samples": []}
    job_id = uuid.uuid4().hex
    jobs[job_id] = job
    title = d.get("title") or "video"
    if d.get("slides"):
        target, args = run_carousel, (job, d["url"], title, d["slides"], d.get("lang"))
    else:
        target, args = run_job, (job, d["url"], title, d.get("ranges") or [], d.get("quality", "best"), d.get("cookies"), d.get("lang"), d.get("format") or "mp4")
    threading.Thread(target=target, args=args, daemon=True).start()
    return jsonify(id=job_id)


@app.get("/api/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(status="error", error=tr(request.args.get("lang"), "job"))
    # оценка оставшегося времени по скорости роста прогресса за последние ~10 секунд
    now, smp = time.time(), job["samples"]
    smp.append((now, job["progress"]))
    while len(smp) > 1 and now - smp[0][0] > 10:
        smp.pop(0)
    eta = None
    if job["status"] == "running" and len(smp) > 1:
        dt, dp = smp[-1][0] - smp[0][0], smp[-1][1] - smp[0][1]
        if dt > 2 and dp > 0:
            eta = (1 - job["progress"]) * dt / dp
    out = {k: v for k, v in job.items() if k != "samples"}
    out["eta"] = eta
    return jsonify(out)


@app.post("/api/quit")
def quit_app():
    threading.Timer(0.4, lambda: os._exit(0)).start()
    return jsonify(ok=True)


@app.get("/files/<path:name>")
def files(name):
    return send_from_directory(OUT, name, as_attachment=True)


def open_ui():
    """Окно приложения: Edge/Chrome в режиме --app (без вкладок и адресной строки), иначе обычный браузер."""
    roots = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    cands = [shutil.which("msedge"), shutil.which("chrome")]
    cands += [os.path.join(r, *p) for r in roots if r for p in (("Microsoft", "Edge", "Application", "msedge.exe"),
                                                                 ("Google", "Chrome", "Application", "chrome.exe"))]
    for exe in cands:
        if exe and os.path.exists(exe):
            subprocess.Popen([exe, f"--app={URL}", "--window-size=960,900"])
            return
    webbrowser.open(URL)


def tray():
    """Значок в трее (Открыть / Выход). Нужны pystray и pillow; без них просто возвращаем False."""
    try:
        import pystray
        from PIL import Image
        img = Image.open(os.path.join(BASE_DIR, "icon.ico"))
    except Exception:  # noqa: BLE001
        return False
    menu = pystray.Menu(
        pystray.MenuItem("Open", lambda icon, item: open_ui(), default=True),
        pystray.MenuItem("Quit", lambda icon, item: (icon.stop(), os._exit(0))),
    )
    pystray.Icon("yt-downloader", img, "YT Downloader", menu).run()
    return True


if __name__ == "__main__":
    with socket.socket() as sk:
        sk.settimeout(0.3)
        already_running = sk.connect_ex(("127.0.0.1", 5000)) == 0
    if already_running:  # вторая копия не нужна: просто открываем окно
        open_ui()
        sys.exit(0)

    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # не засорять лог запросами статуса
    if not (shutil.which("deno") or shutil.which("node") or shutil.which("bun")):
        print("!!! JavaScript runtime (Deno) not found: YouTube will not download. Put deno.exe into the bin folder.")
    if not shutil.which("ffmpeg"):
        print("!!! ffmpeg not found. Put ffmpeg.exe into the bin folder.")
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=5000), daemon=True).start()
    threading.Timer(1.0, open_ui).start()
    if not tray():
        while True:
            time.sleep(3600)
