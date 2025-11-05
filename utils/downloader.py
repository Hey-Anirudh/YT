import os
import yt_dlp
import asyncio
import time
import warnings

DOWNLOAD_FOLDER = "downloads"
COOKIE_FILE = "cookies/cookie.txt"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs("cookies", exist_ok=True)

def _build_base_opts(outtmpl: str, extra: dict | None = None):
    opts = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "socket_timeout": 30,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    if extra:
        opts.update(extra)
    if os.path.exists(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    return opts

async def _run_ydl(opts: dict, url: str):
    loop = asyncio.get_event_loop()
    def _download():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.download([url])
    return await loop.run_in_executor(None, _download)

async def download(video_id: str, type_: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    if type_ == "audio":
        file_path = os.path.join(DOWNLOAD_FOLDER, f"{video_id}.webm")
        if os.path.exists(file_path):
            return file_path
        tries = [
            _build_base_opts(file_path, {"format": "bestaudio[abr<=128]/bestaudio"}),
            _build_base_opts(file_path, {"format": "bestaudio/best"}),
            _build_base_opts(file_path, {"format": "worstaudio/worst"}),
        ]
    elif type_ == "video":
        file_path = os.path.join(DOWNLOAD_FOLDER, f"{video_id}.mp4")
        if os.path.exists(file_path):
            return file_path
        tries = [
            _build_base_opts(file_path, {"format": "bestvideo[height<=720]+bestaudio/best", "merge_output_format": "mp4"}),
            _build_base_opts(file_path, {"format": "bestvideo+bestaudio/best", "merge_output_format": "mp4"}),
            _build_base_opts(file_path, {"format": "worstvideo/worst+bestaudio/worst", "merge_output_format": "mp4"}),
        ]
    else:
        raise ValueError("Invalid type")

    backoff = [0, 1, 2, 4]
    for attempt_no, base_opts in enumerate(tries):
        for retry_no, delay in enumerate(backoff):
            if delay:
                await asyncio.sleep(delay)
            try:
                await _run_ydl(base_opts, url)
                if os.path.exists(file_path):
                    return file_path
            except Exception as e:
                err = str(e).lower()
                if "requested format is not available" in err or "unable to download webpage" in err and "429" in err:
                    if "requested format is not available" in err:
                        break
                if retry_no == len(backoff) - 1:
                    break
                continue
    raise RuntimeError("attempts failed")
