"""YouTube research MCP - list a channel's videos and pull transcripts, cheaply.

Why this exists: plain web-fetching a YouTube channel page returns the
client-rendered app shell (no video list, no captions), so ordinary research
tools come back empty. This server goes through yt-dlp's extractors instead,
which read YouTube's own internal JSON endpoints. No YouTube Data API key, no
browser rendering, no scraping of rendered HTML.

Everything here is read-only. Nothing is ever downloaded to disk - video
listings come from a flat playlist extraction, and transcripts come from the
caption track URL fetched straight into memory.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

# Works on both major versions of the `mcp` package: 2.x renamed the server
# class (FastMCP -> MCPServer). Nothing else this server uses changed.
try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

mcp = FastMCP("youtube-research")

# Caption formats we can parse, best first. json3 is YouTube's own JSON caption
# format - cleanest to parse and free of the duplicated rolling lines that make
# auto-generated VTT painful.
CAPTION_FORMATS = ("json3", "srv3", "srv1", "vtt")

RSS_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
YT_NS = "{http://www.youtube.com/xml/schemas/2015}"

# Channel tabs that are a list of videos. Anything else gets rewritten to /videos.
VIDEO_TABS = ("videos", "shorts", "streams")


# --------------------------------------------------------------------------
# yt-dlp plumbing
# --------------------------------------------------------------------------

def _ydl_opts(**extra: Any) -> dict:
    """Base yt-dlp options, plus optional cookie passthrough.

    YouTube occasionally demands a signed-in session ("Sign in to confirm you're
    not a bot"). Set YT_DLP_COOKIES_FROM_BROWSER=chrome (or firefox/edge), or
    YT_DLP_COOKIEFILE=/path/to/cookies.txt, and yt-dlp will use it.
    """
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noprogress": True,
        # Off by default so failures raise a DownloadError we can turn into a
        # useful message. Listings pass ignoreerrors=True so one bad entry does
        # not kill a whole channel listing.
        "ignoreerrors": False,
        "extractor_retries": 2,
        "socket_timeout": 30,
    }
    browser = os.environ.get("YT_DLP_COOKIES_FROM_BROWSER")
    if browser:
        opts["cookiesfrombrowser"] = (browser.strip().lower(),)
    cookiefile = os.environ.get("YT_DLP_COOKIEFILE")
    if cookiefile:
        opts["cookiefile"] = cookiefile
    opts.update(extra)
    return opts


def _extract(url: str, **extra: Any) -> dict:
    """Run one yt-dlp extraction and raise a readable error if it comes back empty."""
    with yt_dlp.YoutubeDL(_ydl_opts(**extra)) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError(_friendly_error(url, None))
    return info


def _friendly_error(url: str, err: Optional[BaseException]) -> str:
    text = str(err) if err else ""
    low = text.lower()
    if "sign in to confirm" in low or "bot" in low and "confirm" in low:
        return (
            f"YouTube asked this machine to sign in before serving {url}. Set the "
            "env var YT_DLP_COOKIES_FROM_BROWSER=chrome (or firefox/edge) on the MCP "
            "server registration and retry."
        )
    if "private" in low:
        return f"{url} is private - no listing or captions are available."
    if "unavailable" in low or "removed" in low:
        return f"{url} is unavailable or has been removed."
    if "does not exist" in low or "not found" in low or "404" in low:
        return f"No such channel/video: {url}. Check the handle or video ID."
    if text:
        return f"yt-dlp could not read {url}: {text.strip()}"
    return (
        f"yt-dlp returned nothing for {url}. If this is a channel, confirm the handle "
        "is spelled right; if a video, confirm it is public."
    )


# --------------------------------------------------------------------------
# URL normalisation
# --------------------------------------------------------------------------

def _channel_url(channel: str) -> str:
    """Turn anything channel-shaped into a URL yt-dlp can list.

    Accepts: '@handle', 'handle', a /channel/UC... id, any youtube.com channel
    URL with or without a tab, and playlist URLs (passed through untouched).
    """
    c = (channel or "").strip()
    if not c:
        raise ValueError("channel_url is empty - pass a handle like '@mwganson' or a channel URL.")

    if not re.match(r"^https?://", c, re.I):
        if c.startswith("@"):
            return f"https://www.youtube.com/{c}/videos"
        if re.fullmatch(r"UC[\w-]{22}", c):
            return f"https://www.youtube.com/channel/{c}/videos"
        if re.match(r"^(www\.|m\.)?youtube\.com/", c, re.I):
            c = "https://" + c.lstrip("/")
        else:
            return f"https://www.youtube.com/@{c}/videos"

    parsed = urlparse(c)
    # Playlists have their own extractor - leave them exactly as given.
    if "list" in parse_qs(parsed.query) or parsed.path.startswith("/playlist"):
        return c

    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        raise ValueError(f"{c} is not a channel URL.")
    if parts[-1] in VIDEO_TABS:
        tab = parts[-1]
        parts = parts[:-1]
    else:
        # /featured, /about, /playlists, /community, or a bare channel root
        if parts[-1] in ("featured", "about", "playlists", "community", "search"):
            parts = parts[:-1]
        tab = "videos"
    return f"https://www.youtube.com/{'/'.join(parts)}/{tab}"


def _video_id(video: str) -> str:
    """Extract an 11-character video ID from an ID or any YouTube video URL."""
    v = (video or "").strip()
    if not v:
        raise ValueError("video_url_or_id is empty.")
    if re.fullmatch(r"[\w-]{11}", v):
        return v

    if not re.match(r"^https?://", v, re.I):
        v = "https://" + v.lstrip("/")
    parsed = urlparse(v)
    qs = parse_qs(parsed.query)
    if qs.get("v"):
        candidate = qs["v"][0]
        if re.fullmatch(r"[\w-]{11}", candidate):
            return candidate
    parts = [p for p in parsed.path.split("/") if p]
    if parsed.netloc.endswith("youtu.be") and parts:
        candidate = parts[0]
    elif len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
        candidate = parts[1]
    elif len(parts) == 1:
        candidate = parts[0]
    else:
        candidate = ""
    if re.fullmatch(r"[\w-]{11}", candidate):
        return candidate
    raise ValueError(
        f"Could not find a video ID in '{video}'. Pass an 11-character ID or a "
        "youtube.com/watch?v=... / youtu.be/... / /shorts/... URL."
    )


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------

def _hms(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _iso_date(compact: Optional[str]) -> Optional[str]:
    """yt-dlp gives upload_date as YYYYMMDD; return YYYY-MM-DD."""
    if compact and re.fullmatch(r"\d{8}", str(compact)):
        c = str(compact)
        return f"{c[:4]}-{c[4:6]}-{c[6:]}"
    return compact


def _entry_to_video(e: dict) -> dict:
    return {
        "video_id": e.get("id"),
        "title": e.get("title"),
        "url": e.get("url") or (f"https://www.youtube.com/watch?v={e.get('id')}" if e.get("id") else None),
        "duration_seconds": e.get("duration"),
        "duration": _hms(e.get("duration")),
        "view_count": e.get("view_count"),
        "upload_date": None,  # filled in below when available
    }


# --------------------------------------------------------------------------
# Upload dates
# --------------------------------------------------------------------------

def _rss_dates(channel_id: Optional[str]) -> dict[str, str]:
    """Upload dates for a channel's ~15 most recent videos, in one cheap request.

    A flat playlist extraction does not carry upload dates, and fetching each
    video's metadata individually is slow. The channel's public RSS feed has
    exact publish dates for recent uploads, which covers most research use.
    """
    if not channel_id:
        return {}
    try:
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            raw = ydl.urlopen(RSS_FEED.format(channel_id)).read()
        root = ET.fromstring(raw)
    except Exception:
        return {}  # best effort only - never fail a listing over a missing date
    dates: dict[str, str] = {}
    for entry in root.findall(f"{ATOM_NS}entry"):
        vid = entry.findtext(f"{YT_NS}videoId")
        published = entry.findtext(f"{ATOM_NS}published")
        if vid and published:
            dates[vid] = published[:10]
    return dates


def _one_video_date(video_id: str) -> tuple[str, Optional[str]]:
    try:
        info = _extract(f"https://www.youtube.com/watch?v={video_id}", extract_flat=False)
        return video_id, _iso_date(info.get("upload_date"))
    except Exception:
        return video_id, None


def _fill_all_dates(videos: list[dict]) -> None:
    """Resolve every missing upload date with a per-video metadata fetch."""
    missing = [v["video_id"] for v in videos if not v.get("upload_date") and v.get("video_id")]
    if not missing:
        return
    with ThreadPoolExecutor(max_workers=4) as pool:
        resolved = dict(pool.map(_one_video_date, missing))
    for v in videos:
        if not v.get("upload_date"):
            v["upload_date"] = resolved.get(v["video_id"])


# --------------------------------------------------------------------------
# Caption parsing
# --------------------------------------------------------------------------

def _pick_track(info: dict, language: str) -> tuple[Optional[dict], str, str]:
    """Choose the best caption track: manual subtitles beat auto-generated ones.

    Returns (track_dict, kind, language_code_used).
    """
    for kind, key in (("manual", "subtitles"), ("automatic", "automatic_captions")):
        tracks: dict = info.get(key) or {}
        if not tracks:
            continue
        # Exact match, then a regional variant (en-US for 'en'), then anything.
        candidates = [language]
        candidates += sorted(k for k in tracks if k.split("-")[0] == language and k != language)
        candidates += sorted(k for k in tracks if k not in candidates)
        for code in candidates:
            fmts = tracks.get(code)
            if not fmts:
                continue
            by_ext = {f.get("ext"): f for f in fmts if f.get("url")}
            for ext in CAPTION_FORMATS:
                if ext in by_ext:
                    return by_ext[ext], kind, code
    return None, "", ""


def _parse_json3(payload: dict) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    for event in payload.get("events") or []:
        segs = event.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if text:
            out.append((event.get("tStartMs", 0) / 1000.0, text))
    return out


_VTT_TIME = re.compile(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->")
_VTT_TAG = re.compile(r"<[^>]+>")


def _parse_timed_text(raw: str) -> list[tuple[float, str]]:
    """Parse VTT/SRT-ish or srv1/srv3 XML caption text into (start_seconds, text)."""
    stripped = raw.lstrip()
    if stripped.startswith("<"):
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return []
        out: list[tuple[float, str]] = []
        for node in root.iter():
            if node.tag not in ("text", "p"):
                continue
            start = node.get("start") or node.get("t")
            if start is None:
                continue
            seconds = float(start) / (1000.0 if node.get("t") is not None else 1.0)
            text = "".join(node.itertext()).replace("\n", " ").strip()
            if text:
                out.append((seconds, text))
        return out

    out = []
    start: Optional[float] = None
    buf: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        m = _VTT_TIME.search(line)
        if m:
            if start is not None and buf:
                out.append((start, " ".join(buf)))
            h, mm, ss, ms = (int(g) for g in m.groups())
            start, buf = h * 3600 + mm * 60 + ss + ms / 1000.0, []
            continue
        clean = _VTT_TAG.sub("", line).strip()
        if not clean or clean in ("WEBVTT",) or clean.startswith(("Kind:", "Language:", "NOTE")):
            continue
        # Auto-generated VTT repeats the previous line as a rolling caption.
        if clean in seen:
            continue
        seen.add(clean)
        buf.append(clean)
    if start is not None and buf:
        out.append((start, " ".join(buf)))
    return out


def _fetch_caption_lines(track: dict) -> list[tuple[float, str]]:
    with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
        raw = ydl.urlopen(track["url"]).read()
    if track.get("ext") == "json3":
        import json

        return _parse_json3(json.loads(raw.decode("utf-8", "replace")))
    return _parse_timed_text(raw.decode("utf-8", "replace"))


def _to_blocks(lines: list[tuple[float, str]], block_seconds: int, timestamps: bool) -> str:
    """Group caption fragments into readable ~block_seconds paragraphs."""
    if not lines:
        return ""
    blocks: list[str] = []
    block_start = lines[0][0]
    buf: list[str] = []
    for start, text in lines:
        if buf and start - block_start >= block_seconds:
            prefix = f"[{_hms(block_start)}] " if timestamps else ""
            blocks.append(prefix + " ".join(buf))
            block_start, buf = start, []
        buf.append(text)
    if buf:
        prefix = f"[{_hms(block_start)}] " if timestamps else ""
        blocks.append(prefix + " ".join(buf))
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@mcp.tool(
    name="list_channel_videos",
    annotations={
        "title": "List a YouTube channel's videos",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def list_channel_videos(
    channel_url: str,
    max_results: int = 50,
    resolve_all_dates: bool = False,
) -> dict:
    """List a YouTube channel's uploads, newest first, without an API key.

    Use this to survey what a channel has published before deciding which videos
    are worth transcribing. Also accepts playlist URLs.

    Args:
        channel_url: A handle ('@mwganson'), a bare name ('mwganson'), a channel
            ID ('UCxxxx...'), any youtube.com channel URL (with or without a
            /videos, /shorts or /streams tab), or a playlist URL.
        max_results: How many videos to return, newest first (1-1000, default 50).
        resolve_all_dates: False (default) fills upload dates for roughly the 15
            most recent videos from the channel's RSS feed, which is one extra
            cheap request. True fetches every listed video's metadata to date the
            whole list - accurate but roughly one request per video, so only use
            it on small max_results.

    Returns:
        {
          "channel": str | None,        # display name
          "channel_id": str | None,     # UC... id
          "channel_url": str,           # URL actually listed
          "total_videos": int | None,   # total on the channel/playlist, if known
          "count": int,                 # videos in this response
          "videos": [
            {
              "video_id": str,            # e.g. "Tbiu_rMJolk"
              "title": str,
              "url": str,                 # watch URL, feed straight to get_video_transcript
              "duration_seconds": int | None,
              "duration": str | None,     # "44:19"
              "view_count": int | None,
              "upload_date": str | None   # "2022-08-14", None if not resolved
            }
          ]
        }

    Errors:
        Raises ValueError for an unparseable channel reference and RuntimeError
        with an actionable message if YouTube refuses the listing.
    """
    if max_results < 1 or max_results > 1000:
        raise ValueError("max_results must be between 1 and 1000.")
    url = _channel_url(channel_url)

    try:
        info = _extract(url, extract_flat="in_playlist", playlistend=max_results, ignoreerrors=True)
    except (DownloadError, ExtractorError) as e:
        raise RuntimeError(_friendly_error(url, e)) from e

    entries = [e for e in (info.get("entries") or []) if e]
    videos = [_entry_to_video(e) for e in entries][:max_results]

    channel_id = info.get("channel_id")
    for vid, date in _rss_dates(channel_id).items():
        for v in videos:
            if v["video_id"] == vid:
                v["upload_date"] = date
    if resolve_all_dates:
        _fill_all_dates(videos)

    return {
        "channel": info.get("channel") or info.get("uploader") or info.get("title"),
        "channel_id": channel_id,
        "channel_url": url,
        "total_videos": info.get("playlist_count"),
        "count": len(videos),
        "videos": videos,
    }


@mcp.tool(
    name="get_video_transcript",
    annotations={
        "title": "Get a YouTube video transcript",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def get_video_transcript(
    video_url_or_id: str,
    language: str = "en",
    include_timestamps: bool = False,
    max_chars: int = 0,
) -> dict:
    """Fetch a video's transcript as clean plain text. No download, no API key.

    Prefers a human-written caption track and falls back to YouTube's
    auto-generated one. Nothing is written to disk - the caption track is read
    straight into memory.

    Args:
        video_url_or_id: An 11-character video ID, or any watch/youtu.be/shorts/
            live/embed URL.
        language: Preferred caption language code (default 'en'). Regional
            variants match too ('en' will accept 'en-US'); if the language is
            missing entirely, the first available track is used.
        include_timestamps: True prefixes each paragraph with [H:MM:SS], which is
            what you want when you intend to cite a moment in the video.
        max_chars: Truncate the transcript at this many characters (0 = no limit).
            Set it when scanning many videos, since a long tutorial can run tens
            of thousands of characters.

    Returns:
        {
          "video_id": str,
          "title": str | None,
          "channel": str | None,
          "url": str,
          "duration_seconds": int | None,
          "duration": str | None,
          "upload_date": str | None,      # "2022-08-14"
          "transcript_kind": str,         # "manual" or "automatic"
          "language": str,                # track actually used, e.g. "en"
          "char_count": int,
          "truncated": bool,
          "transcript": str               # blank-line separated ~30s paragraphs
        }

    Errors:
        Raises ValueError for an unparseable video reference, and RuntimeError
        when the video is unavailable or has no caption track at all (some
        videos genuinely have none - listen for that message rather than
        retrying).
    """
    vid = _video_id(video_url_or_id)
    url = f"https://www.youtube.com/watch?v={vid}"
    try:
        info = _extract(url, extract_flat=False, writesubtitles=True, writeautomaticsub=True)
    except (DownloadError, ExtractorError) as e:
        raise RuntimeError(_friendly_error(url, e)) from e

    track, kind, code = _pick_track(info, language)
    if not track:
        raise RuntimeError(
            f"No caption track exists for {url} ('{info.get('title')}'). Neither the "
            "uploader nor YouTube's auto-captioning produced one, so there is no "
            "transcript to fetch."
        )

    lines = _fetch_caption_lines(track)
    if not lines:
        raise RuntimeError(
            f"The {kind} '{code}' caption track for {url} downloaded but parsed empty "
            f"(format: {track.get('ext')}). Try a different language code."
        )

    text = _to_blocks(lines, block_seconds=30, timestamps=include_timestamps)
    truncated = bool(max_chars) and len(text) > max_chars
    if truncated:
        text = text[:max_chars].rstrip() + "\n\n[truncated]"

    return {
        "video_id": vid,
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "url": url,
        "duration_seconds": info.get("duration"),
        "duration": _hms(info.get("duration")),
        "upload_date": _iso_date(info.get("upload_date")),
        "transcript_kind": kind,
        "language": code,
        "char_count": len(text),
        "truncated": truncated,
        "transcript": text,
    }


@mcp.tool(
    name="search_youtube",
    annotations={
        "title": "Search YouTube",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def search_youtube(query: str, max_results: int = 20) -> dict:
    """Search YouTube by keyword when you do not yet know the channel or video.

    The entry point for research that starts from a topic rather than a URL: find
    candidate videos here, then feed their channel or URL to list_channel_videos
    or get_video_transcript.

    Args:
        query: Free-text search, e.g. "FreeCAD sketcher constraints tutorial".
        max_results: How many results to return (1-100, default 20).

    Returns:
        {
          "query": str,
          "count": int,
          "videos": [ {video_id, title, url, duration_seconds, duration,
                       view_count, channel} ]
        }
        Search results carry no upload date; call get_video_transcript or
        list_channel_videos if you need one.

    Errors:
        Raises ValueError on an empty query and RuntimeError if the search fails.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("query is empty.")
    if max_results < 1 or max_results > 100:
        raise ValueError("max_results must be between 1 and 100.")

    try:
        info = _extract(f"ytsearch{max_results}:{q}", extract_flat="in_playlist")
    except (DownloadError, ExtractorError) as e:
        raise RuntimeError(_friendly_error(f"search '{q}'", e)) from e

    videos = []
    for e in (info.get("entries") or []):
        if not e:
            continue
        v = _entry_to_video(e)
        v.pop("upload_date", None)
        v["channel"] = e.get("channel") or e.get("uploader")
        videos.append(v)

    return {"query": q, "count": len(videos), "videos": videos}


if __name__ == "__main__":
    mcp.run()
