import os
import re
import json
import html as html_mod
import asyncio
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import yt_dlp

MAX_FILE_SIZE      = 2000 * 1024 * 1024
COMPRESS_THRESHOLD = 1900 * 1024 * 1024

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

PLATFORM_MAP = {
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "instagram.com": "Instagram",
    "tiktok.com": "TikTok", "vm.tiktok.com": "TikTok", "vt.tiktok.com": "TikTok",
    "twitter.com": "Twitter/X", "x.com": "Twitter/X",
    "facebook.com": "Facebook", "fb.watch": "Facebook",
    "reddit.com": "Reddit", "v.redd.it": "Reddit",
    "dailymotion.com": "Dailymotion",
    "vimeo.com": "Vimeo",
    "twitch.tv": "Twitch",
    "pinterest.com": "Pinterest",
    "linkedin.com": "LinkedIn",
    "snapchat.com": "Snapchat",
    "rumble.com": "Rumble",
    "bilibili.com": "Bilibili",
    "streamable.com": "Streamable",
    "medal.tv": "Medal",
    "triller.co": "Triller",
    "likee.video": "Likee",
    "coub.com": "Coub",
}

IMPERSONATE_DOMAINS = (
    "twitter.com", "x.com",
    "facebook.com", "fb.watch",
    "pinterest.com", "snapchat.com", "linkedin.com",
)

try:
    import curl_cffi  # noqa: F401
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    for domain, name in PLATFORM_MAP.items():
        if domain in url_lower:
            return name
    return "Video"


def _is_tiktok(url: str) -> bool:
    return any(d in url.lower() for d in ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com"))


def _is_instagram(url: str) -> bool:
    return "instagram.com" in url.lower()


def _needs_impersonation(url: str) -> bool:
    return HAS_CURL_CFFI and any(d in url.lower() for d in IMPERSONATE_DOMAINS)


def _resolve_first_redirect(url: str) -> str:
    class _OneHop(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_OneHop)
    req = urllib.request.Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        ),
    })
    try:
        opener.open(req, timeout=10)
        return url
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location")
        return location if location else url
    except Exception:
        return url


def _extract_thumbnail(video_file: str) -> str | None:
    thumb = video_file.rsplit(".", 1)[0] + "_thumb.jpg"
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", video_file],
            capture_output=True, text=True, timeout=10,
        )
        duration = float(json.loads(probe.stdout).get("format", {}).get("duration", 0))
        seek = "00:00:01" if duration > 1 else "00:00:00"

        subprocess.run(
            ["ffmpeg", "-i", video_file, "-ss", seek, "-vframes", "1", "-q:v", "2", "-y", thumb],
            capture_output=True, timeout=30, check=True,
        )
        if os.path.exists(thumb) and os.path.getsize(thumb) > 0:
            return thumb
    except Exception:
        pass
    return None


def _compress_video(input_file: str, target_bytes: int = COMPRESS_THRESHOLD) -> str:
    output = input_file.rsplit(".", 1)[0] + "_c.mp4"

    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", input_file],
        capture_output=True, text=True, timeout=30, check=True,
    )
    duration = float(json.loads(probe.stdout).get("format", {}).get("duration", 0))
    if duration <= 0:
        raise ValueError("Cannot determine video duration for compression.")

    target_bits = target_bytes * 8 * 0.95
    audio_bits  = 96 * 1024 * duration
    video_kbps  = max(200, int((target_bits - audio_bits) / duration / 1000))

    subprocess.run(
        [
            "ffmpeg", "-i", input_file,
            "-c:v", "libx264", "-b:v", f"{video_kbps}k",
            "-maxrate", f"{int(video_kbps * 1.5)}k",
            "-bufsize", f"{video_kbps * 2}k",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", "-y", output,
        ],
        check=True, capture_output=True, timeout=1800,
    )
    os.remove(input_file)
    return output


def _http_get(url: str, headers: dict = None, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _safe_title(title: str, maxlen: int = 50) -> str:
    return "".join(c for c in title[:maxlen] if c.isalnum() or c in " -_").strip()


def _tikwm_api(url: str) -> dict:
    actual = url
    if "vt.tiktok.com" in url.lower() or "vm.tiktok.com" in url.lower():
        actual = _resolve_first_redirect(url)

    body = urllib.parse.urlencode({"url": actual, "hd": 1}).encode()
    req = urllib.request.Request(
        "https://www.tikwm.com/api/",
        data=body,
        headers={
            "User-Agent":   BROWSER_UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer":      "https://www.tikwm.com/",
        },
    )
    data = json.loads(urllib.request.urlopen(req, timeout=20).read())
    if data.get("code") != 0 or not data.get("data"):
        raise ValueError(data.get("msg", "TikTok API returned no data."))
    return data["data"]


def _tikwm_info(url: str) -> dict:
    d = _tikwm_api(url)
    return {
        "title":    d.get("title", "TikTok Video"),
        "duration": d.get("duration", 0),
        "uploader": d.get("author", {}).get("unique_id", ""),
        "thumbnail": d.get("cover"),
        "platform": "TikTok",
    }


def _tikwm_download(url: str, output_dir: str) -> dict:
    d = _tikwm_api(url)

    video_url = d.get("hdplay") or d.get("play")
    if not video_url:
        raise ValueError("No download URL returned by TikTok API.")

    title    = d.get("title", "TikTok Video")
    filename = os.path.join(output_dir, f"{_safe_title(title) or 'tiktok'}.mp4")

    data = _http_get(video_url, headers={"User-Agent": BROWSER_UA, "Referer": "https://www.tiktok.com/"}, timeout=300)
    with open(filename, "wb") as f:
        f.write(data)

    file_size = os.path.getsize(filename)
    if file_size > COMPRESS_THRESHOLD:
        filename  = _compress_video(filename)
        file_size = os.path.getsize(filename)

    return {
        "title": title, "duration": d.get("duration", 0),
        "platform": "TikTok", "filename": filename, "file_size": file_size,
        "thumbnail": d.get("cover"), "thumbnail_path": None,
    }


def _instagram_embed_download(url: str, output_dir: str) -> dict:
    sc = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
    if not sc:
        raise ValueError("Could not find Instagram post ID in URL.")
    shortcode = sc.group(1)

    page = None
    for path in (f"reel/{shortcode}/embed/captioned/", f"p/{shortcode}/embed/captioned/"):
        try:
            page = _http_get(
                f"https://www.instagram.com/{path}",
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36"
                    ),
                    "Accept":          "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer":         "https://www.instagram.com/",
                },
                timeout=15,
            ).decode("utf-8", errors="ignore")
            break
        except Exception:
            continue

    if not page:
        raise ValueError(
            "Instagram: could not reach embed page.\n"
            "The post may be private or Instagram is blocking access."
        )

    video_match = (
        re.search(r'"video_url":"(https:[^"]+\.mp4[^"]*)"', page) or
        re.search(r'<video[^>]+src="(https://[^"]+)"', page) or
        re.search(r'data-video-url="(https://[^"]+)"', page)
    )
    if not video_match:
        raise ValueError(
            "Instagram download failed — the video could not be extracted.\n"
            "Instagram restricts most Reels without a logged-in account.\n"
            "Consider asking the sender to share the file directly."
        )

    video_url = html_mod.unescape(
        video_match.group(1).replace("\\/", "/").replace("\\u002F", "/")
    )

    title_match = (
        re.search(r'"text":"([^"]{1,300})"', page) or
        re.search(r"<title>([^<]+)</title>", page)
    )
    title = html_mod.unescape(title_match.group(1)[:100]) if title_match else "Instagram Video"

    thumb_match = re.search(r'"display_url":"(https:[^"]+)"', page)
    thumbnail = (
        html_mod.unescape(thumb_match.group(1).replace("\\u002F", "/"))
        if thumb_match else None
    )

    filename = os.path.join(output_dir, f"{_safe_title(title) or 'instagram'}.mp4")
    data = _http_get(video_url, headers={"User-Agent": BROWSER_UA, "Referer": "https://www.instagram.com/"}, timeout=300)
    with open(filename, "wb") as f:
        f.write(data)

    file_size = os.path.getsize(filename)
    if file_size > COMPRESS_THRESHOLD:
        filename  = _compress_video(filename)
        file_size = os.path.getsize(filename)

    return {
        "title": title, "duration": 0, "platform": "Instagram",
        "filename": filename, "file_size": file_size,
        "thumbnail": thumbnail, "thumbnail_path": None,
    }


def _make_ytdlp_opts(output_dir: str, progress_hook=None, impersonate: bool = False) -> dict:
    opts = {
        "format": (
            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]"
            "/best[height<=1080]/best"
        ),
        "outtmpl":             os.path.join(output_dir, "%(title).60s.%(ext)s"),
        "noplaylist":          True,
        "quiet":               True,
        "no_warnings":         True,
        "merge_output_format": "mp4",
        "writethumbnail":      True,
        "postprocessors": [
            {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
        ],
        "http_headers": {
            "User-Agent":      BROWSER_UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    if impersonate and HAS_CURL_CFFI:
        opts["impersonate"] = "chrome"
    return opts


def _ytdlp_info(url: str) -> dict:
    needs_imp = _needs_impersonation(url)
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "http_headers": {"User-Agent": BROWSER_UA},
    }
    if needs_imp and HAS_CURL_CFFI:
        opts["impersonate"] = "chrome"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            raise ValueError("Could not extract video info.")
        if "entries" in info:
            info = info["entries"][0]
        return {
            "title":    info.get("title", "Video"),
            "duration": info.get("duration", 0),
            "uploader": info.get("uploader") or info.get("channel", ""),
            "thumbnail": info.get("thumbnail"),
            "platform": detect_platform(url),
        }


def _ytdlp_download(url: str, output_dir: str, tracker=None) -> dict:
    opts = _make_ytdlp_opts(output_dir, tracker.hook if tracker else None, impersonate=_needs_impersonation(url))

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise ValueError("Could not download video.")
        if "entries" in info:
            info = info["entries"][0]

        filename = ydl.prepare_filename(info)
        if not os.path.exists(filename):
            filename = filename.rsplit(".", 1)[0] + ".mp4"

        thumb_path = None
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            candidate = filename.rsplit(".", 1)[0] + ext
            if os.path.exists(candidate):
                thumb_path = candidate
                break

        file_size = os.path.getsize(filename) if os.path.exists(filename) else 0

        if file_size > MAX_FILE_SIZE:
            raise ValueError(
                f"Video is {file_size // 1024 // 1024} MB — exceeds Telegram's 2 GB limit. "
                "Try a shorter clip."
            )
        if file_size > COMPRESS_THRESHOLD:
            filename  = _compress_video(filename)
            file_size = os.path.getsize(filename)

        return {
            "title":          info.get("title", "Video"),
            "duration":       info.get("duration", 0),
            "platform":       detect_platform(url),
            "filename":       filename,
            "file_size":      file_size,
            "thumbnail":      info.get("thumbnail"),
            "thumbnail_path": thumb_path,
        }


class ProgressTracker:
    def __init__(self):
        self.percent = 0
        self.speed   = ""
        self.eta     = ""

    def hook(self, d: dict):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            if total:
                self.percent = int(d.get("downloaded_bytes", 0) / total * 100)
            self.speed = d.get("_speed_str", "")
            self.eta   = d.get("_eta_str",   "")
        elif d["status"] == "finished":
            self.percent = 100


async def get_video_info(url: str) -> dict:
    loop = asyncio.get_event_loop()
    if _is_tiktok(url):
        return await loop.run_in_executor(None, _tikwm_info, url)
    if _is_instagram(url):
        try:
            return await loop.run_in_executor(None, _ytdlp_info, url)
        except Exception:
            return {"title": "Instagram Video", "duration": 0, "platform": "Instagram"}
    return await loop.run_in_executor(None, _ytdlp_info, url)


async def download_video(url: str, output_dir: str, tracker: ProgressTracker = None) -> dict:
    loop = asyncio.get_event_loop()
    try:
        if _is_tiktok(url):
            result = await loop.run_in_executor(None, _tikwm_download, url, output_dir)

        elif _is_instagram(url):
            try:
                result = await loop.run_in_executor(None, _ytdlp_download, url, output_dir, tracker)
            except Exception as e:
                err = str(e).lower()
                if any(k in err for k in ("login", "private", "403", "401", "not found", "unable to extract", "blocked")):
                    result = await loop.run_in_executor(None, _instagram_embed_download, url, output_dir)
                else:
                    raise

        else:
            result = await loop.run_in_executor(None, _ytdlp_download, url, output_dir, tracker)

        if not result.get("thumbnail_path"):
            thumb = await loop.run_in_executor(None, _extract_thumbnail, result["filename"])
            result["thumbnail_path"] = thumb

        return result

    except yt_dlp.utils.DownloadError as e:
        raise ValueError(str(e)[:300])
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(str(e)[:300])
