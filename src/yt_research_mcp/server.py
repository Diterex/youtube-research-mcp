"""YouTube research MCP - list a channel's videos and pull transcripts, cheaply.

Why this exists: plain web-fetching a YouTube channel page returns the
client-rendered app shell (no video list, no captions), so ordinary research
tools come back empty. This server goes through yt-dlp's extractors instead,
which read YouTube's own internal JSON endpoints. No YouTube Data API key, no
browser rendering, no scraping of rendered HTML.

Everything here is read-only against YouTube itself: no channel is modified, no
video is uploaded, nothing is posted anywhere. Disk use is minimal and
deliberate - video listings and transcripts never touch disk (a flat playlist
extraction and a caption track read straight into memory), and get_video_frames
downloads only a small video-only stream to a temp file it deletes on exit,
never the full video with audio. The one deliberate exception is
get_video_frames' optional output_dir: when a caller explicitly asks, extracted
frame JPEGs (never the video itself) are written to a caller-chosen local
directory instead of only being returned in-context - opt-in, and nothing else
in this server persists anything anywhere.
"""

from __future__ import annotations

import atexit
import glob
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import yt_dlp
from yt_dlp.cookies import CookieLoadError
from yt_dlp.utils import YoutubeDLError

# Works on both major versions of the `mcp` package: 2.x renamed the server
# class (FastMCP -> MCPServer). Nothing else this server uses changed.
try:  # mcp >= 2.0
    from mcp.server.mcpserver import Image, MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP("youtube-research")

# Caption formats we can parse, best first. json3 is YouTube's own JSON caption
# format - cleanest to parse and free of the duplicated rolling lines that make
# auto-generated VTT painful.
CAPTION_FORMATS = ("json3", "srv3", "srv1", "vtt")

RSS_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
YT_NS = "{http://www.youtube.com/xml/schemas/2015}"

# When the RSS feed itself is unreachable (not just silent on an old video -
# genuinely down), list_channel_videos falls back to resolving dates one video
# at a time via yt-dlp instead, same path resolve_all_dates uses. Capped so a
# large max_results doesn't turn a feed outage into up to 1000 serial fetches;
# ~20 roughly matches what the feed would have covered for free on a healthy
# day, so the default call degrades to parity rather than losing dates outright.
_RSS_FALLBACK_CAP = 20

# Channel tabs that are a list of videos. Anything else gets rewritten to /videos.
VIDEO_TABS = ("videos", "shorts", "streams")

# Hosts a full URL is allowed to point at. Anything else is rejected outright -
# this server hands URLs to yt-dlp's extractor, which has a generic fallback
# extractor capable of fetching arbitrary URLs (not just YouTube's own). Without
# this check, a caller (including an LLM agent acting on untrusted content it
# read elsewhere) could pass a non-YouTube URL - e.g. a playlist-shaped one
# pointing at an internal host - and have this server make an outbound request
# to it. Every code path that accepts a full URL must check this before doing
# anything else with the URL.
_ALLOWED_HOSTS = ("youtube.com", "youtu.be")


def _is_youtube_host(netloc: str) -> bool:
    host = netloc.split("@")[-1].split(":")[0].lower()  # strip userinfo/port
    return any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS)


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
    # Checked by type, not text: CookieLoadError's own message is just "failed to
    # load cookies" - the useful detail (e.g. "could not copy Chrome cookie
    # database") only ever reaches yt-dlp's logger, never the exception itself.
    if isinstance(err, CookieLoadError) or "could not copy" in low and "cookie" in low:
        return (
            f"Could not read cookies for {url}: the browser named in "
            "YT_DLP_COOKIES_FROM_BROWSER is currently running and holds an exclusive "
            "lock on its own cookie database (confirmed on Windows: this fails while "
            "Chrome is open, works once it's closed). Close that browser and retry, "
            "point YT_DLP_COOKIES_FROM_BROWSER at a browser that's currently closed, "
            "or switch to YT_DLP_COOKIEFILE with an exported cookies.txt instead - "
            "that path doesn't depend on any browser's run state."
        )
    if "sign in to confirm" in low or "bot" in low and "confirm" in low:
        return (
            f"YouTube asked this machine to sign in before serving {url}. Set the "
            "env var YT_DLP_COOKIES_FROM_BROWSER to a browser that is CURRENTLY CLOSED "
            "(the browser-cookie path fails outright while that browser is running - "
            "see the README) or set YT_DLP_COOKIEFILE to an exported cookies.txt, "
            "which works regardless of what's open, then retry."
        )
    # yt-dlp has no fixed geo-block string of its own - it relays whatever reason
    # text YouTube's backend sends, which is reportedly phrasings like "not
    # available in your country" / "not made this video available in your
    # country". Never actually triggered in testing (checked 2026-08-08: a
    # documented geo-block test video is now blocked everywhere by copyright
    # claim instead, and BBC's YouTube channel turned out not to be region-locked
    # the way BBC iPlayer is) - this branch is a best-effort match on the
    # reported wording, unverified against a real occurrence.
    if "country" in low and ("not available" in low or "not made" in low):
        return (
            f"{url} is blocked in this server's region. There is no cookie or retry "
            "fix for this - it needs a video available where this machine is."
        )
    if "private" in low:
        return f"{url} is private - no listing or captions are available."
    if "unavailable" in low or "removed" in low:
        return f"{url} is unavailable or has been removed."
    if "does not exist" in low or "not found" in low or "404" in low:
        return f"No such channel/video: {url}. Check the handle or video ID."
    if "live stream" in low or "live broadcast" in low:
        return f"{url} is a live broadcast - see the live-stream note above."
    if text:
        return f"yt-dlp could not read {url}: {text.strip()}"
    return (
        f"yt-dlp returned nothing for {url}. If this is a channel, confirm the handle "
        "is spelled right; if a video, confirm it is public."
    )


_PERMANENT_FAILURE_MARKERS = (
    "could not copy", "cookie", "sign in to confirm", "country", "private",
    "unavailable", "removed", "does not exist", "not found", "404",
    "live stream", "live broadcast",
)


def _is_permanent_failure(text: str) -> bool:
    """True if retrying this exact error could never help - only worth checking
    before a retry loop, never a substitute for _friendly_error's actual message.
    A generic HTTP failure (403/5xx/timeout) is presumed transient and IS retried:
    confirmed real 2026-08-08 on a 4-hour video's stream fetch, which 403'd once
    then succeeded seconds later with nothing else changed.
    """
    low = text.lower()
    return any(m in low for m in _PERMANENT_FAILURE_MARKERS)


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
    if not _is_youtube_host(parsed.netloc):
        raise ValueError(
            f"'{c}' does not point at youtube.com or youtu.be. Refusing to pass a "
            "non-YouTube URL to the extractor."
        )
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

def _fetch_rss_bytes(channel_id: str) -> bytes:
    """One HTTP GET of a channel's upload RSS feed. Split out from _rss_dates so
    a test can fake just the network call and pin the retry/fallback logic
    deterministically, the same way _run_search is split out for search_youtube.
    """
    with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
        return ydl.urlopen(RSS_FEED.format(channel_id)).read()


def _rss_dates(channel_id: Optional[str]) -> tuple[dict[str, str], bool]:
    """Upload dates for a channel's ~15 most recent videos, in one cheap request.

    A flat playlist extraction does not carry upload dates, and fetching each
    video's metadata individually is slow (see _fill_all_dates). The channel's
    public RSS feed has exact publish dates for recent uploads, which covers
    most research use - when the feed is actually reachable, which is not
    guaranteed: confirmed live 2026-08-10 that this endpoint can return a bare
    HTTP 500 on one probe and a 404 on the very next one, for the same channel,
    with no other change - not a one-off blip to retry through, an unreliable
    dependency to have a real fallback for.

    Returns (dates, feed_ok). feed_ok is False only when the feed request itself
    failed after retries, or came back as something that isn't parseable feed
    XML (e.g. an HTML error page served with a 200). The caller uses this to
    tell "this video predates the feed's ~15-video window" (feed_ok=True, dates
    just doesn't mention it - normal, no action needed) apart from "the feed is
    down and nothing could be learned from it at all" (feed_ok=False - worth
    falling back on).
    """
    if not channel_id:
        return {}, True

    raw: Optional[bytes] = None
    for attempt in range(3):
        try:
            raw = _fetch_rss_bytes(channel_id)
            break
        except Exception as e:  # noqa: BLE001 - urlopen can raise outside yt-dlp's hierarchy
            if _is_permanent_failure(str(e)) or attempt == 2:
                break
            time.sleep(1.5 * (attempt + 1))
    if raw is None:
        return {}, False

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}, False  # an error page served as the body parses as invalid XML

    dates: dict[str, str] = {}
    for entry in root.findall(f"{ATOM_NS}entry"):
        vid = entry.findtext(f"{YT_NS}videoId")
        published = entry.findtext(f"{ATOM_NS}published")
        if vid and published:
            dates[vid] = published[:10]
    return dates, True


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


def _fetch_url_bytes(url: str) -> bytes:
    """GET a URL with the same retry-transient/skip-permanent policy as the
    video-stream fetch. Caption track URLs are signed and time-limited the same
    way stream URLs are (see _local_stream's docstring) - this closes a gap
    where that class of failure was hardened for frames but not transcripts:
    this call used to be a bare, unprotected urlopen, so any network hiccup
    fetching the caption file crashed the whole tool call with a raw traceback
    instead of a clean error.
    """
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
                return ydl.urlopen(url).read()
        except Exception as e:  # noqa: BLE001 - urlopen can raise outside yt-dlp's own hierarchy
            last_err = e
            if _is_permanent_failure(str(e)) or attempt == 2:
                break
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(_friendly_error(url, last_err)) from last_err


def _fetch_caption_lines(track: dict) -> list[tuple[float, str]]:
    raw = _fetch_url_bytes(track["url"])
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
# Search broadening and relevance scoring
#
# The problem this solves, from a real research session: a narrow,
# academically-phrased query ("additive manufacturing parameter optimisation
# for viscoelastic ceramic feedstock") returned almost nothing, while a blunt
# practitioner phrasing of the same topic ("clay 3d printing") found the best
# material in the whole effort. Nobody should have to remember to re-run the
# blunt version by hand.
#
# HONEST LIMIT, stated up front because it shapes everything below: this module
# is plain Python with no model in it. It cannot invent a synonym, and it cannot
# know that "clay printing" and "viscoelastic ceramic feedstock" are the same
# subject. So the work is split by who can actually do it:
#
#   * Mechanical broadening (this file, always available, zero caller effort):
#     shorten the query. Drop academic register words, keep the head of the noun
#     phrase, keep the lead of it. These are strictly SUBSETS of what the caller
#     already typed - no new vocabulary is invented, because none can be.
#   * Semantic broadening (the CALLER's job, via broader_terms): real synonyms
#     and adjacent tool/software names. The calling session is an LLM and can
#     generate these trivially; this function cannot generate even one. When
#     broader_terms is absent the returned "note" says so explicitly, so the
#     caller learns to supply them next time rather than silently getting the
#     weaker half of the feature.
#   * Re-ranking (this file): score every result by lexical coverage of the
#     topic vocabulary, so genuinely on-topic results float and the noise a
#     deliberately over-broad query drags in gets dropped again.
# --------------------------------------------------------------------------

# Extra searches a single call may run beyond the narrow one. They run in
# parallel, so the wall-clock cost of broadening is roughly one search, but the
# request count is not free - hence a hard cap.
_MAX_BROAD_QUERIES = 4

# Phase-1 trigger. If fewer than _MIN_RELEVANT narrow results score at least
# _RELEVANT_AT, the narrow pass is treated as thin and broadening runs. Zero
# results trivially satisfies this, so both of Jacob's trigger conditions -
# nothing at all, and results that came back but do not look on-topic - go
# through the same check.
_RELEVANT_AT = 0.34
_MIN_RELEVANT = 3

# Phase-3 keep bar for results the user did not ask for. Deliberately HIGHER
# than _RELEVANT_AT: a result surfaced by a query the caller never typed has to
# prove itself harder than one that came back from their own words.
_BROAD_KEEP_AT = 0.5

# Dropped before scoring and before building the shortened variants. Ordinary
# English glue; carries no topic signal in a video title.
_STOPWORDS = frozenset("""
a an the and or of for to in on at by with without from into as is are be been
being how what why when where which who this that these those it its your you
my our their his her i we they do does did can could should would will if but
not no all any some more most much many other another such via using use used
than then so too very just about over under between during within across per
top best new latest guide part
""".split())

# Dropped ONLY when building the shortened "core" variant, never when scoring.
# These are register words: they mark how a topic is being written about rather
# than what the topic is, and they are what makes an academic phrasing miss on a
# platform whose titles are written by practitioners. A result that does mention
# them is still relevant, which is why scoring keeps them.
#
# This is a fixed, closed, domain-independent list. It is NOT a synonym table
# and deliberately never grows into one - a hardcoded synonym table would be a
# fake version of the semantic broadening only the caller can really do.
_REGISTER_WORDS = frozenset("""
analysis analyses approach approaches assessment characterisation
characterization comparative comparison considerations effect effects empirical
evaluation experimental exploration framework implications influence
investigation method methodology methods model modelling modeling novel
optimisation optimization overview parameter parameters principles properties
qualitative quantitative research review strategies strategy studies study
survey systematic techniques theoretical toward towards understanding
""".split())

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'+#-]*")


def _tokens(text: Optional[str]) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _content_terms(text: Optional[str]) -> list[str]:
    """Topic-carrying words, in the order they were written."""
    return [t for t in _tokens(text) if t not in _STOPWORDS and len(t) > 1]


def _stem(token: str) -> str:
    """Crude suffix stripper so 'constraints' matches 'constraint' and
    'printing' matches 'print'. Deliberately not a real stemmer: no dependency,
    no language model, and its failures are all of the same harmless kind -
    two related words that fail to unify simply score as a miss.
    """
    t = token
    if len(t) > 4 and t.endswith("ies"):
        t = t[:-3] + "y"
    for suffix in ("ations", "ation", "ings", "ing", "ers", "er", "ed", "es", "s"):
        if t.endswith(suffix) and len(t) > len(suffix) + 2:
            return t[: -len(suffix)]
    return t


def _stems(tokens: list[str]) -> list[str]:
    return [_stem(t) for t in tokens]


def _term_hits(stem: str, haystack: list[str]) -> bool:
    """One topic word against a result's words. Exact stem match, or - only for
    stems long enough that a coincidence is unlikely - containment either way,
    which recovers pairs the crude stemmer splits ('constrain'/'constraint').
    """
    if stem in haystack:
        return True
    if len(stem) < 5:
        return False
    return any(stem in h or (len(h) >= 5 and h in stem) for h in haystack)


def _group_score(group: list[str], haystack: list[str]) -> float:
    """How much of ONE phrasing of the topic a result covers, 0.0 to 1.0."""
    if not group:
        return 0.0
    unique = list(dict.fromkeys(group))
    matched = sum(1 for s in unique if _term_hits(s, haystack))
    score = matched / len(unique)
    # Small bonus when two topic words appear side by side in the result, which
    # is much stronger evidence than the same two words scattered.
    for a, b in zip(group, group[1:]):
        if any(haystack[i] == a and haystack[i + 1] == b for i in range(len(haystack) - 1)):
            score += 0.08
    return min(1.0, score)


def _topic_groups(query: str, broader_terms: Optional[list[str]]) -> list[list[str]]:
    """The vocabularies a result may satisfy, as lists of stems.

    Group 0 is the caller's original query. Each broader_term the caller passed
    becomes its OWN group, not extra words bolted onto group 0 - because the
    caller is asserting "this is another way of naming the same subject", and a
    result should be able to satisfy it completely on its own. A video titled
    "Clay 3D printing basics" fully covers the group ['clay', '3d', 'print'] and
    scores 1.0, even though it shares no word at all with an academic query.
    Merging everything into one bag would instead have scored it about 0.2 and
    thrown it away - which is exactly the failure this whole feature exists to
    fix.

    The mechanical shortenings do NOT get groups: they are subsets of group 0,
    so anything they surfaced would trivially score 1.0 against itself and the
    filter would stop filtering.
    """
    groups = [_stems(_content_terms(query))]
    for term in broader_terms or []:
        stems = _stems(_content_terms(term))
        if stems:
            groups.append(stems)
    return [g for g in groups if g]


def _relevance(video: dict, groups: list[list[str]]) -> float:
    """Best coverage across all accepted phrasings of the topic.

    The haystack is title + channel name: a channel called "Ceramic 3D Printing"
    is real evidence about a video whose title alone is "Episode 12".
    """
    haystack = _stems(_tokens(f"{video.get('title') or ''} {video.get('channel') or ''}"))
    return max((_group_score(g, haystack) for g in groups), default=0.0)


def _mechanical_variants(query: str) -> list[str]:
    """Shortened rewrites of the query, best first. No new words are invented.

    Three shapes, because there is no reliable way to know which word carries
    the topic without a parser:
      core - everything except academic register words. Mild; keeps breadth.
      head - the last three content words. English noun phrases put the head
             noun last ("...viscoelastic ceramic feedstock").
      lead - the first three content words. Recovers the opposite case, where
             the distinctive token is a product name up front ("FreeCAD ...").
    A query of three content words or fewer cannot be shortened at all; that
    returns an empty list and the caller is told so in the note.
    """
    terms = _content_terms(query)
    core = [t for t in terms if t not in _REGISTER_WORDS]
    variants: list[str] = []
    for candidate in (core, core[-3:], core[:3]):
        if len(candidate) < 2:
            continue
        text = " ".join(candidate)
        if text != " ".join(terms) and text not in variants:
            variants.append(text)
    return variants


def _run_search(query: str, max_results: int) -> list[dict]:
    """One yt-dlp keyword search, normalised to this server's video shape.

    Split out as its own function so the broadening and re-ranking logic can be
    tested without touching the network.
    """
    info = _extract(f"ytsearch{max_results}:{query}", extract_flat="in_playlist")
    videos = []
    for e in info.get("entries") or []:
        if not e:
            continue
        v = _entry_to_video(e)
        v.pop("upload_date", None)
        v["channel"] = e.get("channel") or e.get("uploader")
        v["channel_url"] = e.get("channel_url") or e.get("uploader_url")
        videos.append(v)
    return videos


def _suggested_channels(videos: list[dict], limit: int = 5) -> list[dict]:
    """Channels that scored well more than once - the concrete 'narrow back in'
    handle. Costs no extra request: it is read off results already in hand, and
    feeds straight into list_channel_videos.
    """
    tally: dict[str, dict] = {}
    for v in videos:
        name = v.get("channel")
        if not name or v.get("relevance", 0.0) < _BROAD_KEEP_AT:
            continue
        row = tally.setdefault(name, {"channel": name, "channel_url": v.get("channel_url"), "hits": 0, "_score": 0.0})
        row["hits"] += 1
        row["_score"] += v.get("relevance", 0.0)
    ranked = sorted(
        (r for r in tally.values() if r["hits"] >= 2),
        key=lambda r: (r["hits"], r["_score"]),
        reverse=True,
    )
    for r in ranked:
        r.pop("_score", None)
    return ranked[:limit]


# --------------------------------------------------------------------------
# Frame grabbing
# --------------------------------------------------------------------------

def _parse_timestamp(value: Any) -> float:
    """Accept 754, '754', '12:34' or '1:23:45' and return seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        raise ValueError("Empty timestamp.")
    if ":" in s:
        parts = s.split(":")
        if len(parts) > 3:
            raise ValueError(f"'{s}' is not a timestamp. Use S, M:SS or H:MM:SS.")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            raise ValueError(f"'{s}' is not a timestamp. Use S, M:SS or H:MM:SS.") from None
        total = 0.0
        for n in nums:
            total = total * 60 + n
        return total
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"'{s}' is not a timestamp. Use S, M:SS or H:MM:SS.") from None


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError(
            "ffmpeg is not on PATH, and frame grabbing needs it. Install it with "
            "'winget install Gyan.FFmpeg' and restart the MCP server."
        )
    return exe


# Fetched video-only streams, keyed by (video_id, max_height), value (path, info).
# Kept for the life of the server process so repeated calls on the same video are
# instant, capped so a long research session cannot fill the disk. Caching info
# alongside the path means a cache hit needs zero network calls, not just a
# cheap one - closes a gap where the "fast path" could fail on an unrelated
# metadata refetch even though the file it actually needed was already local.
_STREAM_CACHE: "OrderedDict[tuple[str, int], tuple[str, dict]]" = OrderedDict()
_CACHE_LIMIT = 3
_CACHE_DIR: Optional[str] = None
# MCP's stdio transport is normally one request at a time, but nothing in the
# protocol guarantees a client won't pipeline overlapping tool calls - this lock
# makes the cache's check-then-act sequence atomic either way, for the cost of a
# few lines. Frame extraction itself (the actual CPU/IO-bound work, one ffmpeg
# subprocess per timestamp) stays outside the lock so concurrent calls on
# different videos aren't serialized more than the cache bookkeeping requires.
_CACHE_LOCK = threading.Lock()


def _cache_dir() -> str:
    """Create this run's temp dir, and best-effort sweep any left behind by a
    prior run that didn't exit cleanly (atexit doesn't fire on a forceful kill,
    so a long-lived install could otherwise accumulate one leftover directory
    per crash). Never lets a sweep failure block startup.
    """
    global _CACHE_DIR
    with _CACHE_LOCK:
        if _CACHE_DIR is None:
            parent = tempfile.gettempdir()
            try:
                for name in os.listdir(parent):
                    if name.startswith("yt-research-frames-"):
                        shutil.rmtree(os.path.join(parent, name), ignore_errors=True)
            except OSError:
                pass
            _CACHE_DIR = tempfile.mkdtemp(prefix="yt-research-frames-")
            atexit.register(shutil.rmtree, _CACHE_DIR, True)
        return _CACHE_DIR


def _evict(key: Optional[tuple[str, int]] = None) -> None:
    victim = key if key is not None else next(iter(_STREAM_CACHE))
    entry = _STREAM_CACHE.pop(victim, None)
    path = entry[0] if entry else None
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _local_stream(video_id: str, max_height: int) -> tuple[str, dict]:
    """Fetch the video-only stream to a temp file and return (path, info).

    Why a fetch rather than seeking the remote URL: YouTube binds a stream URL to
    the player client that requested it, and refuses or throttles anyone else -
    handing the URL to ffmpeg gets a 403 or a stall, and yt-dlp's own
    download_ranges hits the same wall because it shells out to ffmpeg too.
    yt-dlp fetching the whole stream itself works because it holds the matching
    client session.

    This is cheap in practice. The stream is video-only (audio is a separate
    track we never ask for), so a 31 minute 720p tutorial is about 32 MB and
    lands in under 10 seconds. Frames then come off the local file instantly.
    The file is deleted when the server exits.
    """
    key = (video_id, max_height)
    with _CACHE_LOCK:
        cached = _STREAM_CACHE.get(key)
        if cached and os.path.exists(cached[0]):
            _STREAM_CACHE.move_to_end(key)
            return cached

    url = f"https://www.youtube.com/watch?v={video_id}"

    # Check live status BEFORE downloading. A currently-live or not-yet-started
    # broadcast has no end, so "download the stream" never finishes cleanly -
    # confirmed live 2026-08-08: it ran ~90s then ffmpeg exited with a bare
    # "code 1" that gives no hint why. Fail in ~1s with a real reason instead.
    try:
        probe = _extract(url, extract_flat=False)
    except YoutubeDLError as e:
        raise RuntimeError(_friendly_error(url, e)) from e
    if probe.get("is_live") or probe.get("live_status") in ("is_live", "is_upcoming"):
        raise RuntimeError(
            f"{url} ('{probe.get('title')}') is a live broadcast with no fixed "
            "end, so there is no finite stream to fetch frames from. If it has "
            "since ended, wait for YouTube to publish the VOD replay and retry - "
            "a 'was_live' video works normally."
        )

    out = os.path.join(_cache_dir(), f"{video_id}-{max_height}.%(ext)s")
    opts = _ydl_opts(
        skip_download=False,
        outtmpl=out,
        # Video-only first: it is a fraction of the size and we never need audio.
        format=(f"bestvideo[height<=?{max_height}][ext=mp4]"
                f"/bestvideo[height<=?{max_height}]"
                f"/best[height<=?{max_height}]/best"),
    )
    # Retries the WHOLE extraction, not just the byte-level fetch: confirmed
    # 2026-08-08 that yt-dlp's own extractor_retries doesn't cover this, because a
    # signed download URL can 403 in a way that only clears on a fresh extraction
    # (which re-signs it) - a bare retry of the same signed URL wouldn't have
    # helped. Real, reproduced case: a 4-hour video's stream 403'd once, then
    # succeeded seconds later with zero other changes. Skipped entirely for
    # failures no retry could ever fix (bot-wall, cookie lock, live stream,
    # private/unavailable, region-block) so those still fail in ~1 attempt, not 3.
    last_err: Optional[YoutubeDLError] = None
    for attempt in range(3):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            last_err = None
            break
        except YoutubeDLError as e:
            last_err = e
            if _is_permanent_failure(str(e)) or attempt == 2:
                break
            time.sleep(2 * (attempt + 1))
    if last_err is not None:
        raise RuntimeError(_friendly_error(url, last_err)) from last_err
    if not info:
        raise RuntimeError(_friendly_error(url, None))

    path = info.get("requested_downloads", [{}])[0].get("filepath")
    if not path or not os.path.exists(path):
        matches = glob.glob(os.path.join(_cache_dir(), f"{video_id}-{max_height}.*"))
        if not matches:
            raise RuntimeError(
                f"yt-dlp reported success for {url} but produced no file. If this "
                "video is a live stream or members-only, frames are not available."
            )
        path = matches[0]

    with _CACHE_LOCK:
        while len(_STREAM_CACHE) >= _CACHE_LIMIT:
            _evict()
        _STREAM_CACHE[key] = (path, info)
    return path, info


def _grab_frame(args: tuple[str, float, int, int]) -> tuple[float, Optional[bytes], Optional[str]]:
    """Pull one JPEG at one timestamp off the local file."""
    path, seconds, width, quality = args
    cmd = [
        _ffmpeg(), "-nostdin", "-loglevel", "error",
        "-ss", f"{seconds:.3f}",   # seek before -i: instant on a local file
        "-i", path,
        "-frames:v", "1",
        "-vf", f"scale={width}:-2:flags=lanczos",
        "-q:v", str(quality),
        "-f", "image2", "-vcodec", "mjpeg",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        return seconds, None, "ffmpeg timed out after 60s"
    if proc.returncode != 0 or not proc.stdout:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return seconds, None, (err[-1] if err else "ffmpeg produced no frame")
    return seconds, proc.stdout, None


def _frame_filename(video_id: str, seconds: float) -> str:
    """video_id is already known filesystem-safe (validated by _video_id's
    [\\w-]{11} match before it ever reaches here). Zero-padded H-M-S, not a
    colon-separated H:MM:SS: Windows rejects ':' in filenames, and padding
    keeps a directory of frames sorted chronologically by name.
    """
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{video_id}_{h:02d}-{m:02d}-{sec:02d}.jpg"


def _save_frame(directory: str, video_id: str, seconds: float, jpeg: bytes) -> tuple[str, Optional[str]]:
    """Write one already-captured frame to disk. Returns (path, error) with
    error None on success. Never raises: a single frame's write failing (full
    disk, permission denied mid-run) must not lose the frames saved before or
    after it in the same call, matching how a single frame's ffmpeg failure
    already can't fail the rest of get_video_frames.
    """
    path = os.path.join(directory, _frame_filename(video_id, seconds))
    try:
        with open(path, "wb") as f:
            f.write(jpeg)
    except OSError as e:
        return path, str(e)
    return path, None


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
          "note": str,                  # non-empty only when date resolution had
                                         # to fall back - see resolve_all_dates
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
        If the RSS feed itself is unreachable (confirmed to happen: it has
        returned bare HTTP 500s and 404s), this falls back automatically to
        resolving dates one video at a time via yt-dlp - same source
        resolve_all_dates uses - for up to the first 20 videos, so a listing
        never silently loses every date to a single feed outage. "note"
        explains when that happened; pass resolve_all_dates=True to lift the
        20-video cap and date everything that way.

    Errors:
        Raises ValueError for an unparseable channel reference and RuntimeError
        with an actionable message if YouTube refuses the listing.
    """
    if max_results < 1 or max_results > 1000:
        raise ValueError("max_results must be between 1 and 1000.")
    url = _channel_url(channel_url)

    try:
        info = _extract(url, extract_flat="in_playlist", playlistend=max_results, ignoreerrors=True)
    except YoutubeDLError as e:
        raise RuntimeError(_friendly_error(url, e)) from e

    entries = [e for e in (info.get("entries") or []) if e]
    videos = [_entry_to_video(e) for e in entries][:max_results]

    channel_id = info.get("channel_id")
    rss_dates, feed_ok = _rss_dates(channel_id)
    for vid, date in rss_dates.items():
        for v in videos:
            if v["video_id"] == vid:
                v["upload_date"] = date

    note = ""
    if resolve_all_dates:
        _fill_all_dates(videos)
    elif not feed_ok and videos:
        fallback = videos[:_RSS_FALLBACK_CAP]
        _fill_all_dates(fallback)
        capped = len(videos) > len(fallback)
        note = (
            "YouTube's upload-date RSS feed did not respond (not just silent on "
            "an old video - the request itself failed), so dates for "
            f"{'the first ' + str(len(fallback)) if capped else 'all'} "
            f"video{'s' if len(fallback) != 1 else ''} were resolved individually "
            "via yt-dlp instead - slower per video, same accuracy."
        )
        if capped:
            note += (
                f" The remaining {len(videos) - len(fallback)} do not have a "
                "resolved date here; pass resolve_all_dates=True to resolve "
                "every video this way."
            )

    return {
        "channel": info.get("channel") or info.get("uploader") or info.get("title"),
        "channel_id": channel_id,
        "channel_url": url,
        "total_videos": info.get("playlist_count"),
        "count": len(videos),
        "note": note,
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
    except YoutubeDLError as e:
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
def search_youtube(
    query: str,
    max_results: int = 20,
    broader_terms: Optional[list[str]] = None,
    auto_broaden: bool = True,
) -> dict:
    """Search YouTube by keyword, widening the search itself when it comes back thin.

    The entry point for research that starts from a topic rather than a URL: find
    candidate videos here, then feed their channel or URL to list_channel_videos
    or get_video_transcript.

    ALWAYS PASS broader_terms. It is the single highest-value thing you can do
    for a research search, and only you can do it. This function is plain Python
    with no model in it: it can shorten your query, but it cannot possibly guess
    that "clay 3D printing", "paste extrusion" and "ceramic additive
    manufacturing" name one subject, or that a question about a CAD operation is
    also answered by videos about a different CAD program that has the same
    operation. You know that; it does not. Give it 2-3 genuinely different
    phrasings - practitioner slang, the blunt everyday name for the thing, and
    the adjacent tool or software names - and it will search them for you and
    fold the good results back in. Real case this exists for: an academic
    phrasing returned almost nothing while "clay 3d printing" found the best
    material of the entire session.

    What it does, in three phases:
      1. Runs your query exactly as written.
      2. If that came back empty OR came back with results that do not look
         on-topic, it widens: your broader_terms first, plus shortened rewrites
         of your own query (register words like "optimization" and "methodology"
         dropped, then the head and the lead of the phrase). Up to four extra
         searches, run in parallel, so widening costs about one search of time.
      3. Narrows back in: every result is scored on how much of the topic
         vocabulary it actually covers, results the wide pass dragged in that
         are off-topic get dropped, and what remains is sorted best-first.
    When phase 1 already looks healthy, phases 2 and 3 do not run and nothing
    extra is fetched.

    Args:
        query: Free-text search, e.g. "FreeCAD sketcher constraints tutorial".
            Write it the way you actually mean it; broadening is handled here.
        max_results: How many results to return (1-100, default 20).
        broader_terms: Alternate phrasings of the SAME topic, e.g.
            ["clay 3d printing", "paste extruder", "ceramic 3d printer"]. Used
            two ways: as extra searches, and as accepted vocabulary when
            scoring, so a video that matches one of these fully is kept even if
            it shares no word with your original query. 2-3 is the sweet spot.
        auto_broaden: True (default) allows phases 2 and 3. Set False only when
            you deliberately want the raw result of this exact query and nothing
            else - it makes the call behave exactly as it did before this
            feature existed.

    Returns:
        {
          "query": str,                  # your original query, unchanged
          "count": int,
          "broadened": bool,             # whether phases 2-3 ran
          "queries_run": [ {"query": str, "kind": str, "results": int} ],
                                         # kind: narrow | caller | mechanical
          "dropped_as_off_topic": int,   # wide-pass results the re-rank rejected
          "low_confidence": bool,        # true only when your query found NOTHING
                                         # and these are best-effort wide hits
          "suggested_channels": [ {"channel", "channel_url", "hits"} ],
                                         # channels that scored well repeatedly -
                                         # feed to list_channel_videos next
          "note": str,                   # what happened and what to do about it
          "videos": [ {video_id, title, url, duration_seconds, duration,
                       view_count, channel, channel_url,
                       relevance,         # 0.0-1.0 topic coverage
                       found_via} ]       # "your query" or the wider query used
        }
        Results are in YouTube's own ranking order when nothing was broadened,
        and in relevance order (ties keeping YouTube's order) when it was.
        Search results carry no upload date; call get_video_transcript or
        list_channel_videos if you need one.

    Errors:
        Raises ValueError on an empty query and RuntimeError if the search of
        your original query fails. A failure in one of the wider searches never
        fails the call - it is reported in "note" instead.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("query is empty.")
    if max_results < 1 or max_results > 100:
        raise ValueError("max_results must be between 1 and 100.")
    terms = [t.strip() for t in (broader_terms or []) if t and t.strip()]

    groups = _topic_groups(q, terms)
    queries_run: list[dict] = []
    notes: list[str] = []

    # ---- Phase 1: the caller's query, exactly as written --------------------
    try:
        narrow = _run_search(q, max_results)
    except YoutubeDLError as e:
        raise RuntimeError(_friendly_error(f"search '{q}'", e)) from e
    queries_run.append({"query": q, "kind": "narrow", "results": len(narrow)})
    for v in narrow:
        v["relevance"] = round(_relevance(v, groups), 3)
        v["found_via"] = "your query"

    relevant = [v for v in narrow if v["relevance"] >= _RELEVANT_AT]
    thin = len(relevant) < _MIN_RELEVANT

    if not auto_broaden or not thin:
        if not auto_broaden:
            notes.append("auto_broaden=False, so only your exact query was run.")
        else:
            notes.append(
                f"Your query returned {len(relevant)} on-topic results, so no "
                "widening was needed and no extra searches were made."
            )
        return {
            "query": q,
            "count": len(narrow),
            "broadened": False,
            "queries_run": queries_run,
            "dropped_as_off_topic": 0,
            "low_confidence": False,
            "suggested_channels": _suggested_channels(narrow),
            "note": " ".join(notes),
            "videos": narrow[:max_results],
        }

    # ---- Phase 2: widen -----------------------------------------------------
    # Caller-supplied phrasings go first: they are the only ones carrying real
    # meaning. Mechanical shortenings fill whatever budget is left.
    candidates = [(t, "caller") for t in terms[:2]]
    candidates += [(m, "mechanical") for m in _mechanical_variants(q)]
    seen_q = {q.lower()}
    plan: list[tuple[str, str]] = []
    for text, kind in candidates:
        if text.lower() in seen_q or len(plan) >= _MAX_BROAD_QUERIES:
            continue
        seen_q.add(text.lower())
        plan.append((text, kind))

    if not plan:
        notes.append(
            f"Your query returned only {len(relevant)} on-topic results and could "
            "not be widened: it is too short to shorten mechanically and no "
            "broader_terms were given. Call again with broader_terms=['...'] - "
            "2-3 blunt or practitioner phrasings of the same topic."
        )
        return {
            "query": q,
            "count": len(narrow),
            "broadened": False,
            "queries_run": queries_run,
            "dropped_as_off_topic": 0,
            "low_confidence": False,
            "suggested_channels": _suggested_channels(narrow),
            "note": " ".join(notes),
            "videos": narrow[:max_results],
        }

    def _one(item: tuple[str, str]) -> tuple[str, str, Any]:
        text, kind = item
        try:
            return text, kind, _run_search(text, max_results)
        except Exception as e:  # noqa: BLE001 - a wide search must never fail the call
            return text, kind, e

    with ThreadPoolExecutor(max_workers=len(plan)) as pool:
        wide_results = list(pool.map(_one, plan))

    # ---- Phase 3: narrow back in --------------------------------------------
    by_id: dict[str, dict] = {}
    for v in narrow:
        if v.get("video_id"):
            by_id[v["video_id"]] = v

    kept_wide: list[dict] = []
    rejected: list[dict] = []
    failed: list[str] = []
    for text, kind, outcome in wide_results:
        if isinstance(outcome, Exception):
            failed.append(text)
            queries_run.append({"query": text, "kind": kind, "results": 0})
            continue
        queries_run.append({"query": text, "kind": kind, "results": len(outcome)})
        for v in outcome:
            vid = v.get("video_id")
            if not vid or vid in by_id:
                continue  # your own query already found it; keep that attribution
            v["relevance"] = round(_relevance(v, groups), 3)
            v["found_via"] = text
            by_id[vid] = v
            # A result from a query the caller never typed has to clear a higher
            # bar than one from their own words. This is what stops an
            # intentionally over-broad pass from filling the answer with noise.
            (kept_wide if v["relevance"] >= _BROAD_KEEP_AT else rejected).append(v)

    merged = narrow + kept_wide
    low_confidence = False
    if not merged and rejected:
        # The caller's query found literally nothing, and nothing from the wide
        # pass cleared the bar either. Returning an empty list here would be the
        # exact failure this feature exists to prevent, so hand back the closest
        # wide hits and label them plainly so the caller knows to distrust them.
        merged = sorted(rejected, key=lambda v: v["relevance"], reverse=True)[:5]
        promoted = {v["video_id"] for v in merged}
        rejected = [v for v in rejected if v["video_id"] not in promoted]
        low_confidence = True

    # Stable sort: equal relevance keeps YouTube's own ordering, and keeps
    # results from the caller's own query ahead of wide-pass results.
    merged.sort(key=lambda v: v["relevance"], reverse=True)
    videos = merged[:max_results]

    notes.append(
        f"Your query returned {len(narrow)} results ({len(relevant)} on-topic), "
        f"which is thin, so {len(plan)} wider search"
        f"{'es were' if len(plan) != 1 else ' was'} run: "
        + "; ".join(f"'{t}' ({k})" for t, k in plan)
        + f". Kept {len(kept_wide)} extra result{'' if len(kept_wide) == 1 else 's'}, "
        f"dropped {len(rejected)} as off-topic."
    )
    if not terms:
        notes.append(
            "No broader_terms were given, so only mechanical shortening of your "
            "own words was possible - no synonyms or adjacent tool names were "
            "tried, because this tool cannot invent them. If these results are "
            "still thin, call again with broader_terms=['...'] naming the same "
            "topic 2-3 different ways."
        )
    if low_confidence:
        notes.append(
            "LOW CONFIDENCE: nothing matched the topic well. These are the "
            "closest wide-pass hits, returned rather than nothing at all - "
            "verify before trusting them."
        )
    if failed:
        notes.append(f"Wider search(es) that failed and were skipped: {', '.join(failed)}.")

    return {
        "query": q,
        "count": len(videos),
        "broadened": True,
        "queries_run": queries_run,
        "dropped_as_off_topic": len(rejected),
        "low_confidence": low_confidence,
        "suggested_channels": _suggested_channels(videos),
        "note": " ".join(notes),
        "videos": videos,
    }


@mcp.tool(
    name="get_video_frames",
    annotations={
        "title": "Grab video frames at chosen moments",
        # False, not True: with output_dir set this writes JPEG files to a
        # caller-chosen local directory. Still opt-in and still never touches
        # YouTube itself - see destructiveHint/idempotentHint below.
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def get_video_frames(
    video_url_or_id: str,
    timestamps: Optional[list[str]] = None,
    every_seconds: int = 0,
    max_frames: int = 6,
    width: int = 1280,
    max_height: int = 720,
    quality: int = 4,
    output_dir: Optional[str] = None,
    include_images: bool = True,
) -> list:
    """See what a video actually shows at chosen moments. Returns real images,
    and can save them to disk in the same call.

    Transcripts cannot capture a screen-based tutorial. "Click this, then drag it
    here" has no referent in text, toolbar clicks are usually silent, typed
    dialog values are rarely spoken, and auto-captions mangle exactly the
    technical terms you need. Use this to look at the moments that matter.

    The intended workflow is two steps, and doing it in this order is what keeps
    it cheap:
      1. get_video_transcript(..., include_timestamps=True) to find WHICH moments
         matter, across as many videos as you like.
      2. get_video_frames(video_id, timestamps=[...]) on just those moments.

    The video-only stream is fetched to a temp file first, then every frame comes
    off it locally. That sounds expensive and is not: video-only means no audio
    track, so a 31 minute 720p tutorial is about 32 MB and lands in under 10
    seconds, and further calls on the same video are instant because the file is
    kept for the life of the server process (3 videos max, deleted on exit).
    Asking for many timestamps in ONE call is therefore much cheaper than many
    calls, and vastly cheaper than one call per frame on different videos.

    Args:
        video_url_or_id: An 11-character video ID or any YouTube video URL.
        timestamps: The moments to capture, as 'S', 'M:SS' or 'H:MM:SS' strings
            (e.g. ["4:12", "11:38", "1:02:05"]). Take these from a timestamped
            transcript.
        every_seconds: Instead of explicit timestamps, sample evenly this many
            seconds apart. Use only when surveying an unfamiliar video; explicit
            timestamps are far cheaper. Ignored if timestamps is given.
        max_frames: Hard cap on frames captured (1-50, default 6). Applies
            whether or not output_dir is set - it bounds the ffmpeg work this
            call does, not just the context cost of returning images.
        width: Output width in pixels (320-1920, default 1280). Do not go below
            about 960 if you need to read menu labels or dialog values.
        max_height: Source stream height to fetch (default 720, which is enough
            to read a CAD toolbar and keeps the fetch small). Raise to 1080 only
            if 720 proves too coarse.
        quality: JPEG quality, 2 is best and 31 is worst (default 4).
        output_dir: A local directory to save each captured frame to as a JPEG,
            e.g. "C:/research/freecad-sketcher". Created if it does not exist.
            Written from the same ffmpeg output already being produced for the
            in-context images - no separate download or re-extraction. Files are
            named "{video_id}_{HH-MM-SS}.jpg"; saving the same timestamp again
            overwrites the same file. None (default) saves nothing.
        include_images: True (default) returns each frame inline as before.
            Set False only when output_dir is given and you don't want the
            context cost of inline images - e.g. saving many frames for later,
            offline use rather than looking at them in this conversation.

    Returns:
        A list whose first item is a text summary (video title, duration, the
        timestamp of each frame, and - when output_dir is set - the path each
        frame was saved to or why it wasn't), followed by one image per
        timestamp if include_images is True. Frames that could not be captured,
        or captured but failed to save, are reported in the summary text rather
        than failing the whole call.

    Errors:
        Raises ValueError for bad arguments or unparseable timestamps (including
        include_images=False with no output_dir, which would return nothing at
        all), and RuntimeError if ffmpeg is missing, the video has no playable
        stream, or output_dir cannot be created.
    """
    vid = _video_id(video_url_or_id)
    if not 1 <= max_frames <= 50:
        raise ValueError("max_frames must be between 1 and 50.")
    if not 320 <= width <= 1920:
        raise ValueError("width must be between 320 and 1920.")
    if not 2 <= quality <= 31:
        raise ValueError("quality must be between 2 (best) and 31 (worst).")
    if not 144 <= max_height <= 2160:
        raise ValueError("max_height must be between 144 and 2160.")
    if not timestamps and every_seconds <= 0:
        raise ValueError(
            "Give either timestamps=['4:12', ...] or every_seconds=N. Prefer "
            "timestamps taken from a timestamped transcript - it is much cheaper."
        )
    if not include_images and not output_dir:
        raise ValueError(
            "include_images=False with no output_dir would return no frames at "
            "all. Set output_dir to save frames to disk, or drop "
            "include_images=False to get them inline."
        )
    if output_dir is not None and not output_dir.strip():
        raise ValueError("output_dir is empty - pass a real directory path or omit it.")
    _ffmpeg()  # fail fast with an install hint rather than after the fetch

    resolved_dir: Optional[str] = None
    if output_dir:
        resolved_dir = os.path.abspath(os.path.expanduser(output_dir.strip()))
        try:
            os.makedirs(resolved_dir, exist_ok=True)
        except OSError as e:
            raise RuntimeError(f"Could not create or use output_dir '{resolved_dir}': {e}") from e

    path, info = _local_stream(vid, max_height)
    duration = info.get("duration")

    if timestamps:
        wanted = [_parse_timestamp(t) for t in timestamps]
    else:
        if not duration:
            raise ValueError(
                "This video reports no duration, so it cannot be sampled evenly. "
                "Pass explicit timestamps instead."
            )
        wanted = [float(s) for s in range(0, int(duration), every_seconds)]

    if duration:
        over = [t for t in wanted if t > duration]
        wanted = [t for t in wanted if t <= duration]
        if not wanted:
            raise ValueError(
                f"Every requested timestamp is past the end of the video "
                f"({_hms(duration)} long)."
            )
    else:
        over = []

    dropped = max(0, len(wanted) - max_frames)
    wanted = sorted(set(wanted))[:max_frames]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            _grab_frame,
            [(path, t, width, quality) for t in wanted],
        ))

    out: list = []
    lines = [
        f"{info.get('title')} [{_hms(duration)}] - frames from "
        f"https://www.youtube.com/watch?v={vid}",
    ]
    saved = 0
    save_failures = 0
    for seconds, jpeg, err in results:
        if not jpeg:
            lines.append(f"  [{_hms(seconds)}] FAILED: {err}")
            continue
        size = f"({len(jpeg) // 1024} KB)"
        if include_images:
            out.append(Image(data=jpeg, format="jpeg"))
        if resolved_dir:
            saved_path, save_err = _save_frame(resolved_dir, vid, seconds, jpeg)
            if save_err:
                save_failures += 1
                lines.append(f"  [{_hms(seconds)}] captured {size} - FAILED TO SAVE: {save_err}")
            else:
                saved += 1
                lines.append(f"  [{_hms(seconds)}] captured {size} -> {saved_path}")
        else:
            lines.append(f"  [{_hms(seconds)}] captured {size}")
    if dropped:
        lines.append(f"  ({dropped} more timestamps dropped by max_frames={max_frames})")
    if over:
        lines.append(f"  ({len(over)} timestamps ignored as past the end of the video)")
    lines.append("Frames are listed in the same order as the timestamps above.")
    if resolved_dir:
        if saved:
            lines.append(f"Saved {saved} frame file{'s' if saved != 1 else ''} to {resolved_dir}")
        if save_failures:
            lines.append(
                f"{save_failures} frame{'s' if save_failures != 1 else ''} captured but could not "
                "be saved to disk - see FAILED TO SAVE lines above."
            )

    return ["\n".join(lines)] + out


def main() -> None:
    """Console-script entry point (registered in pyproject.toml as
    `yt-research-mcp`) and the target of `python -m yt_research_mcp`."""
    mcp.run()


if __name__ == "__main__":
    main()
