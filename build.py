"""Сборка в exe: python build.py  ->  release/YT Downloader/ (exe + bin с ffmpeg и deno)."""
import importlib.util
import io
import os
import shutil
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
NAME = "YT Downloader"
BIN = os.path.join(ROOT, "bin")


def fetch_zip(url):
    print("  downloading", url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(req, timeout=300).read()))


def ensure_bin():
    """ffmpeg и deno скачиваются один раз; можно и просто положить .exe в папку bin руками."""
    os.makedirs(BIN, exist_ok=True)
    if not os.path.exists(os.path.join(BIN, "deno.exe")):
        fetch_zip("https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip").extract("deno.exe", BIN)
    if not os.path.exists(os.path.join(BIN, "ffmpeg.exe")):
        z = fetch_zip("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip")
        for n in z.namelist():
            if n.endswith(("/bin/ffmpeg.exe", "/bin/ffprobe.exe")):
                with z.open(n) as src, open(os.path.join(BIN, os.path.basename(n)), "wb") as dst:
                    shutil.copyfileobj(src, dst)


def build():
    import PyInstaller.__main__ as pyi
    args = ["app.py", "--noconfirm", "--clean", "--onefile", "--noconsole", "--name", NAME, "--icon", "icon.ico",
            "--add-data", "index.html" + os.pathsep + ".", "--add-data", "icon.ico" + os.pathsep + ".",
            "--collect-all", "yt_dlp", "--hidden-import", "pystray._win32"]
    for pkg in ("yt_dlp_ejs", "curl_cffi", "certifi"):
        if importlib.util.find_spec(pkg):
            args += ["--collect-all", pkg]
    pyi.run(args)


if __name__ == "__main__":
    ensure_bin()
    build()
    rel = os.path.join(ROOT, "release", NAME)
    shutil.rmtree(rel, ignore_errors=True)
    os.makedirs(rel)
    shutil.copy(os.path.join(ROOT, "dist", NAME + ".exe"), rel)
    shutil.copytree(BIN, os.path.join(rel, "bin"))
    print("Done:", rel)
