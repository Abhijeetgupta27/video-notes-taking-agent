# everything that talks to youtube: metadata, transcript, stream url, frame grabs
import json
import logging
import re
import subprocess

import redis
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from youtube_transcript_api import (AgeRestricted, IpBlocked, NoTranscriptFound, PoTokenRequired,
                                    RequestBlocked, TranscriptsDisabled, VideoUnavailable,
                                    VideoUnplayable, YouTubeRequestFailed, YouTubeTranscriptApi)
from youtube_transcript_api.proxies import GenericProxyConfig

from common import config
from common.ratelimit import Limiter

from .errors import Fatal, RateLimited, Retryable, chaos

log = logging.getLogger("worker.yt")
limiter = Limiter(redis.Redis.from_url(config.REDIS_URL, decode_responses=True))

EN = ["en", "en-US", "en-GB", "en-IN", "en-CA", "en-AU"]


class FrameError(Exception):
    pass


def _slot():
    limiter.take("youtube", 60, max_n=config.YT_RPM)
    chaos("youtube")


def _opts(**extra):
    o = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
         "socket_timeout": 20, "retries": 2}
    if config.YT_PROXY:
        o["proxy"] = config.YT_PROXY
    if config.YT_COOKIES:
        o["cookiefile"] = config.YT_COOKIES
    o.update(extra)
    return o


def _classify(e):
    s = str(e)
    low = s.lower()
    if "429" in low or "too many requests" in low:
        return RateLimited("youtube", "YouTube returned 429 Too Many Requests")
    if "private video" in low:
        return Retryable("VIDEO_UNAVAILABLE", "This video is private")
    if "confirm you" in low and "bot" in low:
        return Retryable("YOUTUBE_BLOCKED", "YouTube wants a sign-in to prove we're not a bot (see YT_COOKIES in README)")
    for bit in ("video unavailable", "not available", "has been removed", "members-only", "join this channel",
                "confirm your age", "terminated"):
        if bit in low:
            return Retryable("VIDEO_UNAVAILABLE", "Video is unavailable (removed, private, region or age locked)")
    line = s.replace("ERROR:", "").strip().splitlines()[0] if s.strip() else "unknown yt-dlp error"
    line = re.sub(r"^\[youtube\]\s*[\w-]+:\s*", "", line)[:160]
    return Retryable("YOUTUBE_ERROR", line)


def _extract(vid, **opts):
    _slot()
    try:
        with YoutubeDL(_opts(**opts)) as y:
            return y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
    except DownloadError as e:
        raise _classify(e)


def meta(vid):
    info = _extract(vid, ignore_no_formats_error=True)
    if info.get("availability") in ("private", "needs_auth", "subscriber_only", "premium_only"):
        raise Retryable("VIDEO_UNAVAILABLE", f"Video is not publicly available ({info['availability']})")
    return {
        "title": info.get("title") or vid,
        "duration": info.get("duration"),
        "is_live": bool(info.get("is_live")),
        "channel": info.get("channel") or info.get("uploader"),
        "subs": _pick_subs(info),
    }


def _pick_subs(info):
    # json3 caption url from yt-dlp, used if youtube-transcript-api gets blocked
    for group in ("subtitles", "automatic_captions"):
        tracks = info.get(group) or {}
        langs = [l for l in EN if l in tracks] + [l for l in tracks if l.startswith("en")] + \
                [l for l in tracks if l.endswith("-orig")]
        for lang in langs:
            for t in tracks[lang]:
                if t.get("ext") == "json3":
                    return {"lang": lang, "url": t["url"]}
    return None


# --- transcript

def transcript(vid, m):
    """returns (lines, note). lines = [[start_sec, text], ...]"""
    try:
        return _transcript_api(vid)
    except (TranscriptsDisabled, NoTranscriptFound):
        pass  # maybe yt-dlp still sees auto captions
    except (VideoUnavailable, VideoUnplayable, AgeRestricted):
        raise Retryable("VIDEO_UNAVAILABLE", "Video is unavailable for transcripts")
    except YouTubeRequestFailed as e:
        if "429" in str(e):
            raise RateLimited("youtube", "YouTube returned 429 while fetching the transcript")
        log.warning("transcript api failed for %s: %s", vid, str(e)[:200])
    except (RequestBlocked, IpBlocked, PoTokenRequired):
        log.warning("transcript api blocked for %s, trying yt-dlp captions", vid)
    except RateLimited:
        raise
    except Exception as e:
        log.warning("transcript api error for %s: %s", vid, str(e)[:200])

    if m.get("subs"):
        lines = _json3(m["subs"]["url"])
        if lines:
            note = None if m["subs"]["lang"].startswith("en") else f"captions are in '{m['subs']['lang']}'"
            return lines, note
    raise Fatal("NO_TRANSCRIPT", "This video has no captions/transcript, so there's nothing to summarize")


def _transcript_api(vid):
    _slot()
    px = GenericProxyConfig(http_url=config.YT_PROXY, https_url=config.YT_PROXY) if config.YT_PROXY else None
    api = YouTubeTranscriptApi(proxy_config=px)
    tl = api.list(vid)
    note = None
    try:
        t = tl.find_transcript(EN)
    except NoTranscriptFound:
        t = next(iter(tl))
        if t.is_translatable:
            try:
                t = t.translate("en")
                note = "transcript was auto-translated to English"
            except Exception:
                note = f"transcript is in '{t.language_code}'"
    fetched = t.fetch()
    lines = [[round(s.start, 2), s.text.replace("\n", " ").strip()] for s in fetched if s.text.strip()]
    return lines, note


def _json3(url):
    _slot()
    try:
        with YoutubeDL(_opts()) as y:
            raw = y.urlopen(url).read()
    except Exception as e:
        if "429" in str(e):
            raise RateLimited("youtube", "YouTube returned 429 while fetching captions")
        log.warning("json3 captions failed: %s", str(e)[:200])
        return []
    out = []
    for ev in json.loads(raw).get("events", []):
        segs = ev.get("segs")
        if not segs or "tStartMs" not in ev:
            continue
        txt = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if txt:
            out.append([round(ev["tStartMs"] / 1000, 2), txt])
    return out


# --- frames

FMT = ("bv*[height<=720][protocol=https]/b[height<=720][protocol=https]/"
       "bv*[height<=720]/b[height<=720]/bv*/b")


def stream_url(vid):
    info = _extract(vid, format=FMT)
    f = info
    if not f.get("url") and info.get("requested_formats"):
        f = info["requested_formats"][0]
    if not f.get("url"):
        raise Retryable("NO_STREAM", "Couldn't find a playable video stream")
    return f["url"], f.get("http_headers") or {}


def download_low(vid, folder):
    """fallback when youtube refuses plain ffmpeg reads of the stream (403): let yt-dlp fetch a
    small <=480p video-only copy with its own downloader, frames are then cut from that file"""
    _slot()
    opts = _opts(skip_download=False, noprogress=True, overwrites=True,
                 format="bv*[height<=480][ext=mp4]/bv*[height<=480]/b[height<=480]/worst",
                 outtmpl=f"{folder}/%(id)s.%(ext)s", concurrent_fragment_downloads=4)
    try:
        with YoutubeDL(opts) as y:
            info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)
            path = (info.get("requested_downloads") or [{}])[0].get("filepath") or y.prepare_filename(info)
    except DownloadError as e:
        raise _classify(e)
    return path


def grab_frame(url, headers, ts):
    """seek the real stream and pull exactly one frame as jpeg bytes"""
    chaos("youtube")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if headers:
        cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    cmd += ["-ss", f"{ts:.2f}", "-i", url, "-frames:v", "1",
            "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=config.FRAME_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise FrameError("ffmpeg timed out")
    img = p.stdout
    if p.returncode != 0 or len(img) < 1500 or not img.startswith(b"\xff\xd8"):
        err = p.stderr.decode(errors="ignore").strip()
        if "429" in err:
            raise RateLimited("youtube", "YouTube returned 429 while grabbing a frame")
        raise FrameError(err.splitlines()[-1][:200] if err else "ffmpeg returned no image")
    return img
