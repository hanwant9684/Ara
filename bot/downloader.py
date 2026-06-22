import os
import re
import json
import html as html_mod
import asyncio
import logging
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import yt_dlp
import instaloader

logger = logging.getLogger(__name__)

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
    "terabox.com": "Terabox",
    "1024terabox.com": "Terabox",
    "freeterabox.com": "Terabox",
    "4funbox.com": "Terabox",
    "teraboxapp.com": "Terabox",
    "mirrobox.com": "Terabox",
    "nephobox.com": "Terabox",
    "momerybox.com": "Terabox",
    "tibibox.com": "Terabox",
}

IMPERSONATE_DOMAINS = (
    "twitter.com", "x.com",
    "facebook.com", "fb.watch",
    "pinterest.com", "snapchat.com", "linkedin.com",
)

TERABOX_DOMAINS = (
    "terabox.com", "1024terabox.com", "freeterabox.com",
    "4funbox.com", "teraboxapp.com", "mirrobox.com",
    "nephobox.com", "momerybox.com", "tibibox.com",
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


def _is_terabox(url: str) -> bool:
    return any(d in url.lower() for d in TERABOX_DOMAINS)


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


# ── TikTok ───────────────────────────────────────────────────────────────────

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


# ── Instagram (instaloader + session auth + embed fallback) ──────────────────

INSTALOADER_SESSION_FILE = os.path.join(
    os.path.dirname(__file__), "..", ".instagram_session"
)
_INSTALOADER_INSTANCE: instaloader.Instaloader | None = None


def _build_instaloader() -> instaloader.Instaloader:
    return instaloader.Instaloader(
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        quiet=True,
        request_timeout=30,
    )


def _get_instaloader() -> instaloader.Instaloader:
    """Return a logged-in Instaloader instance, cached for the process lifetime."""
    global _INSTALOADER_INSTANCE
    if _INSTALOADER_INSTANCE is not None:
        return _INSTALOADER_INSTANCE

    username     = os.environ.get("INSTAGRAM_USERNAME", "").strip()
    password     = os.environ.get("INSTAGRAM_PASSWORD", "").strip()
    session_id   = urllib.parse.unquote(os.environ.get("INSTAGRAM_SESSION_ID", "").strip())
    session_file = os.path.abspath(INSTALOADER_SESSION_FILE)

    L = _build_instaloader()

    # 1. Try loading saved session file first (fastest — avoids re-auth on restart)
    if username and os.path.exists(session_file):
        try:
            L.load_session_from_file(username, session_file)
            test_user = L.context.test_login()   # real server-side check
            if test_user:
                L.context.username = test_user
                logger.info("Instagram: session file valid — logged in as @%s", test_user)
                _INSTALOADER_INSTANCE = L
                return L
            else:
                logger.warning("Instagram: session file exists but is expired/invalid, re-authenticating")
                L = _build_instaloader()
        except Exception as e:
            logger.warning("Instagram: session file load failed (%s), will re-authenticate", e)
            L = _build_instaloader()

    # 2. Inject INSTAGRAM_SESSION_ID cookie
    if session_id:
        try:
            L.context._session.cookies.set(
                "sessionid", session_id, domain=".instagram.com", path="/"
            )
            L.context._session.cookies.set(
                "ig_did", "bot", domain=".instagram.com", path="/"
            )
            test_user = L.context.test_login()
            if test_user:
                L.context.username = test_user
                L.save_session_to_file(session_file)
                logger.info("Instagram: session ID valid — logged in as @%s, session saved", test_user)
                _INSTALOADER_INSTANCE = L
                return L
            else:
                logger.warning("Instagram: session ID did not authenticate, may be expired")
                L = _build_instaloader()
        except Exception as e:
            logger.warning("Instagram: session ID injection failed (%s)", e)
            L = _build_instaloader()

    # 3. Fall back to username + password login
    if username and password:
        try:
            L.login(username, password)
            L.save_session_to_file(session_file)
            logger.info("Instagram: logged in as @%s via password, session saved", username)
            _INSTALOADER_INSTANCE = L
            return L
        except instaloader.exceptions.BadCredentialsException:
            logger.error("Instagram: bad credentials — check INSTAGRAM_USERNAME/PASSWORD")
        except instaloader.exceptions.TwoFactorAuthRequiredException:
            logger.error("Instagram: 2FA is enabled — disable it or use INSTAGRAM_SESSION_ID instead")
        except Exception as e:
            logger.warning("Instagram: password login failed (%s)", e)

    # 4. Anonymous fallback (works for some public posts)
    logger.warning("Instagram: no valid auth — running anonymously, downloads may fail")
    _INSTALOADER_INSTANCE = L
    return L


def _download_ig_node(node, idx: int, output_dir: str) -> dict:
    """Download a single Instagram post node (video or image).
    Works for both Post and PostSidecarNode objects."""
    if node.is_video:
        url  = node.video_url
        ext  = ".mp4"
        kind = "video"
    else:
        # PostSidecarNode uses display_url; Post uses url
        url  = getattr(node, "display_url", None) or getattr(node, "url", None)
        ext  = ".jpg"
        kind = "photo"

    filename = os.path.join(output_dir, f"instagram_{idx}{ext}")
    data = _http_get(
        url,
        headers={"User-Agent": BROWSER_UA, "Referer": "https://www.instagram.com/"},
        timeout=300,
    )
    with open(filename, "wb") as f:
        f.write(data)

    file_size = os.path.getsize(filename)
    if kind == "video" and file_size > COMPRESS_THRESHOLD:
        filename  = _compress_video(filename)
        file_size = os.path.getsize(filename)

    return {
        "filename":       filename,
        "file_size":      file_size,
        "is_video":       kind == "video",
        "thumbnail_path": None,
    }


def _instaloader_download(url: str, output_dir: str) -> dict:
    sc = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
    if not sc:
        raise ValueError("Could not find Instagram post shortcode in URL.")
    shortcode = sc.group(1)

    L = _get_instaloader()

    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
    except instaloader.exceptions.LoginRequiredException:
        raise _InstagramAuthError("This post requires login.")
    except Exception as e:
        raise _InstagramAuthError(f"Could not fetch post metadata — {e}")

    caption = post.caption or ""
    title   = caption[:100].split("\n")[0].strip() if caption else "Instagram Post"

    # ── Carousel / sidecar (multiple images or videos) ───────────────────────
    if post.typename == "GraphSidecar":
        nodes = list(post.get_sidecar_nodes())
        files = []
        for i, node in enumerate(nodes):
            try:
                files.append(_download_ig_node(node, i + 1, output_dir))
            except Exception as e:
                logger.warning("Instagram: skipping carousel node %d — %s", i + 1, e)

        if not files:
            raise ValueError("Instagram: could not download any items from this carousel.")

        total_size = sum(f["file_size"] for f in files)
        return {
            "title":          title,
            "duration":       0,
            "platform":       "Instagram",
            "filename":       files[0]["filename"],
            "file_size":      total_size,
            "thumbnail":      post.url,
            "thumbnail_path": None,
            "files":          files,
        }

    # ── Single video ─────────────────────────────────────────────────────────
    if post.is_video:
        video_url = post.video_url
        if not video_url:
            raise _InstagramAuthError("instaloader returned no video URL for this post.")

        file_info = _download_ig_node(post, 1, output_dir)
        return {
            "title":          title,
            "duration":       post.video_duration or 0,
            "platform":       "Instagram",
            "filename":       file_info["filename"],
            "file_size":      file_info["file_size"],
            "thumbnail":      post.url,
            "thumbnail_path": None,
            "files":          [file_info],
        }

    # ── Single image ─────────────────────────────────────────────────────────
    file_info = _download_ig_node(post, 1, output_dir)
    return {
        "title":          title,
        "duration":       0,
        "platform":       "Instagram",
        "filename":       file_info["filename"],
        "file_size":      file_info["file_size"],
        "thumbnail":      post.url,
        "thumbnail_path": None,
        "files":          [file_info],
    }


class _InstagramAuthError(Exception):
    """Raised when instaloader fails due to auth/block — triggers embed fallback."""


def _instagram_embed_download(url: str, output_dir: str) -> dict:
    """Best-effort fallback: extract video from Instagram's public embed page."""
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
            "Instagram: could not reach the embed page.\n"
            "The post may be private or Instagram is blocking access."
        )

    video_match = (
        re.search(r'"video_url":"(https:[^"]+\.mp4[^"]*)"', page) or
        re.search(r'<video[^>]+src="(https://[^"]+)"', page) or
        re.search(r'data-video-url="(https://[^"]+)"', page)
    )
    if not video_match:
        raise ValueError(
            "Instagram download failed — the video could not be extracted from the embed page.\n"
            "The post may require a logged-in account."
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
    data = _http_get(
        video_url,
        headers={"User-Agent": BROWSER_UA, "Referer": "https://www.instagram.com/"},
        timeout=300,
    )
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


# ── Terabox ──────────────────────────────────────────────────────────────────

def _terabox_extract_surl(url: str) -> str:
    m = re.search(r"/s/([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]surl=([A-Za-z0-9_%-]+)", url)
    if m:
        return urllib.parse.unquote(m.group(1))
    raise ValueError("Could not extract Terabox share key from URL.")


def _terabox_api(surl: str) -> dict:
    params = urllib.parse.urlencode({
        "app_id": "250528",
        "shorturl": surl,
        "root": "1",
    })
    headers = {
        "User-Agent": BROWSER_UA,
        "Referer":    "https://www.terabox.com/",
        "Accept":     "application/json, text/plain, */*",
    }
    url = f"https://www.terabox.com/api/shorturlinfo?{params}"
    raw = _http_get(url, headers=headers, timeout=20)
    data = json.loads(raw)
    if data.get("errno", -1) != 0:
        raise ValueError(f"Terabox API error: {data.get('errmsg', 'unknown error')}")
    file_list = data.get("list", [])
    if not file_list:
        raise ValueError("Terabox returned an empty file list.")
    return file_list[0]


def _terabox_get_dlink(fs_id: str, surl: str) -> str:
    params = urllib.parse.urlencode({
        "app_id":  "250528",
        "shorturl": surl,
        "fid_list": f"[{fs_id}]",
    })
    headers = {
        "User-Agent": BROWSER_UA,
        "Referer":    "https://www.terabox.com/",
    }
    raw = _http_get(
        f"https://www.terabox.com/api/shorturlinfo?{params}&need_download_link=1",
        headers=headers, timeout=20,
    )
    data = json.loads(raw)
    dlink = (data.get("dlink") or "").strip()
    if not dlink and data.get("list"):
        dlink = (data["list"][0].get("dlink") or "").strip()
    if not dlink:
        raise ValueError("Terabox: could not retrieve a download link for this file.")
    return dlink


def _terabox_download(url: str, output_dir: str) -> dict:
    surl     = _terabox_extract_surl(url)
    fileinfo = _terabox_api(surl)

    server_filename = fileinfo.get("server_filename", "terabox_video.mp4")
    fs_id           = str(fileinfo.get("fs_id", ""))
    file_size_remote = int(fileinfo.get("size", 0))

    category = int(fileinfo.get("category", 0))
    if category not in (1, 3):
        raise ValueError(
            f"Terabox: this file doesn't appear to be a video (category={category}).\n"
            "Only video files are supported."
        )

    ext      = os.path.splitext(server_filename)[1] or ".mp4"
    title    = os.path.splitext(server_filename)[0]
    filename = os.path.join(output_dir, f"{_safe_title(title) or 'terabox'}{ext}")

    dlink = _terabox_get_dlink(fs_id, surl)

    req = urllib.request.Request(dlink, headers={
        "User-Agent": BROWSER_UA,
        "Referer":    "https://www.terabox.com/",
    })
    with urllib.request.urlopen(req, timeout=600) as resp:
        with open(filename, "wb") as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)

    file_size = os.path.getsize(filename)
    if file_size > MAX_FILE_SIZE:
        raise ValueError(
            f"Terabox file is {file_size // 1024 // 1024} MB — exceeds Telegram's 2 GB limit."
        )
    if file_size > COMPRESS_THRESHOLD:
        filename  = _compress_video(filename)
        file_size = os.path.getsize(filename)

    return {
        "title":          title,
        "duration":       0,
        "platform":       "Terabox",
        "filename":       filename,
        "file_size":      file_size,
        "thumbnail":      None,
        "thumbnail_path": None,
    }


# ── yt-dlp (generic) ─────────────────────────────────────────────────────────

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


# ── Progress tracker ─────────────────────────────────────────────────────────

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


# ── Public API ────────────────────────────────────────────────────────────────

async def get_video_info(url: str) -> dict:
    loop = asyncio.get_event_loop()
    if _is_tiktok(url):
        return await loop.run_in_executor(None, _tikwm_info, url)
    if _is_instagram(url):
        return {"title": "Instagram Video", "duration": 0, "platform": "Instagram"}
    if _is_terabox(url):
        return {"title": "Terabox Video", "duration": 0, "platform": "Terabox"}
    return await loop.run_in_executor(None, _ytdlp_info, url)


async def download_video(url: str, output_dir: str, tracker: ProgressTracker = None) -> dict:
    loop = asyncio.get_event_loop()
    try:
        if _is_tiktok(url):
            result = await loop.run_in_executor(None, _tikwm_download, url, output_dir)

        elif _is_instagram(url):
            try:
                result = await loop.run_in_executor(None, _instaloader_download, url, output_dir)
            except _InstagramAuthError as e:
                logger.warning("Instagram instaloader failed (%s), trying embed fallback", e)
                result = await loop.run_in_executor(None, _instagram_embed_download, url, output_dir)

        elif _is_terabox(url):
            result = await loop.run_in_executor(None, _terabox_download, url, output_dir)

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
