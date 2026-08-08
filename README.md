# youtube-research-mcp

An MCP server that lets Claude research YouTube channels and read video transcripts,
without a YouTube API key and without rendering a browser.

## Why it exists

Fetching a YouTube channel page the ordinary way returns the app shell only. YouTube
builds its pages in the browser with JavaScript, so a plain fetch gets an empty
skeleton with no video list and no captions in it. That is what made an earlier
attempt to mine FreeCAD tutorial channels come back with nothing usable.

This server goes around that. It uses [yt-dlp](https://github.com/yt-dlp/yt-dlp),
which talks to YouTube's own internal JSON endpoints, so it gets the real data
directly. No API key, no browser, no scraping of rendered pages.

Everything is read only. Video listings come from a flat playlist extraction (titles
and IDs only, no video data), and transcripts are read straight from the caption track
into memory. Only `get_video_frames` touches the disk, and only a temp file it deletes
when the server exits.

### Prior art, and why this exists anyway

There are more than forty YouTube MCP servers out there, so this was checked before
going further. The result was a clean split: **every server that works without an API
key handles one video at a time, and every server that lists a channel needs a Google
Data API key.** [kevinwatt/yt-dlp-mcp](https://github.com/kevinwatt/yt-dlp-mcp) (MIT,
266 stars, the best maintained) has no channel enumeration at all.
[labeveryday/youtube-mcp-server-enhanced](https://github.com/labeveryday/youtube-mcp-server-enhanced)
advertises channel support, but reading its extractor shows `get_channel_info()`
returns subscriber and view counts, not a video list.
[ZubeidHendricks/youtube-mcp-server](https://github.com/ZubeidHendricks/youtube-mcp-server)
and [space-cadet/yt-mcp](https://space-cadet.github.io/yt-mcp/) do list channels, and
both require an API key. Licenses were all MIT, so nothing here was a copyleft problem
either way. Nothing was close enough to fork.

## Tools

### `list_channel_videos(channel_url, max_results=50, resolve_all_dates=False)`

Lists a channel's uploads, newest first.

- `channel_url` accepts a handle (`@mwganson`), a bare name (`mwganson`), a channel
  ID (`UCLNPmhURJNIm9wsRunKM8mA`), any youtube.com channel URL with or without a
  `/videos`, `/shorts` or `/streams` tab, or a playlist URL.
- `max_results` is 1 to 1000, default 50.
- `resolve_all_dates` controls how upload dates are filled in. See the note below.

Returns `channel`, `channel_id`, `channel_url`, `total_videos`, `count`, and a
`videos` list. Each video has `video_id`, `title`, `url`, `duration_seconds`,
`duration`, `view_count`, and `upload_date`.

### `get_video_transcript(video_url_or_id, language="en", include_timestamps=False, max_chars=0)`

Fetches a video's transcript as clean plain text.

- `video_url_or_id` accepts an 11 character video ID or any watch, youtu.be, shorts,
  live or embed URL.
- `language` is the preferred caption language code. Regional variants match too, so
  `en` will accept `en-US`. If the language is missing entirely, the first available
  track is used.
- `include_timestamps` puts `[H:MM:SS]` at the start of each paragraph, which is what
  you want if you plan to cite a moment in the video.
- `max_chars` truncates the result. 0 means no limit. Set it when you are scanning
  many videos, because a long tutorial can run tens of thousands of characters.

A human written caption track is preferred, and YouTube's auto generated one is the
fallback. The result reports which you got in `transcript_kind`, along with `title`,
`channel`, `duration`, `upload_date`, `language`, `char_count`, `truncated`, and the
`transcript` itself as paragraphs of roughly 30 seconds each.

### `get_video_frames(video_url_or_id, timestamps=None, every_seconds=0, max_frames=6, width=1280, max_height=720, quality=4)`

Returns actual images of what the video shows at chosen moments.

**Transcripts alone cannot capture a screen based tutorial.** "Click this, then drag it
here" has no referent in text. Toolbar clicks are usually silent. Typed dialog values
are rarely spoken. And auto captions mangle exactly the technical terms you need, which
you can see for yourself in the transcripts this tool returns, where PDWrapper comes
through as "pt wrapper" and FreeCAD as "free cad".

So the intended workflow is two tiers, and doing it in this order is what keeps it
cheap.

1. **Broad and cheap.** `get_video_transcript(..., include_timestamps=True)` across as
   many videos as you like, to find which videos and which minutes matter.
2. **Narrow and visual.** `get_video_frames(video_id, timestamps=[...])` on just those
   moments.

- `timestamps` is a list of `'S'`, `'M:SS'` or `'H:MM:SS'` strings, taken from step 1.
- `every_seconds` samples evenly instead, for surveying an unfamiliar video. Explicit
  timestamps are far cheaper.
- `max_frames` caps the result at 1 to 20, default 6. Every frame costs context.
- `width` is the output width, 320 to 1920, default 1280. Do not go below about 960 if
  you need to read menu labels.
- `max_height` is the source stream height fetched, default 720. That is enough to read
  a CAD toolbar and keeps the fetch small.

Returns a text summary followed by one image per timestamp. Frames that fail are noted
in the summary rather than failing the whole call.

### `search_youtube(query, max_results=20)`

Keyword search, for when you do not know the channel or video yet. Returns the same
video fields minus the upload date. Use it to find candidates, then feed a result's
channel or URL to one of the other two tools.

## Install and register

The server has its own virtual environment on purpose, so installing yt-dlp here
cannot disturb the other MCP servers that share `~/.claude/mcp-servers/.venv-mcp2`.

```powershell
cd C:\Users\Diterex\Documents\Claude\youtube-research-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Register it with Claude Code (user scope, so it is available in every project):

```powershell
claude mcp add youtube-research --scope user -- `
  C:\Users\Diterex\Documents\Claude\youtube-research-mcp\.venv\Scripts\python.exe `
  C:\Users\Diterex\Documents\Claude\youtube-research-mcp\server.py
```

Or add it by hand to `~/.claude.json`:

```json
"youtube-research": {
  "type": "stdio",
  "command": "C:\\Users\\Diterex\\Documents\\Claude\\youtube-research-mcp\\.venv\\Scripts\\python.exe",
  "args": ["C:\\Users\\Diterex\\Documents\\Claude\\youtube-research-mcp\\server.py"]
}
```

## Testing

```powershell
.\.venv\Scripts\python.exe test_smoke.py
```

This hits YouTube for real, with no mocking, deliberately - every real failure mode
found and fixed in this project (the cookie-database lock, the live-stream hang, the
transient 403 on a large fetch) only showed up by testing against the real service; a
mocked suite would have asserted the code does what it was written to do, not caught
where that turned out to be wrong. It lists two real channels, pulls a real transcript,
runs a search, checks every URL shape the parsers are supposed to handle, and rejects
whatever's currently live on a 24/7 stream in a couple of seconds rather than
attempting to download it. Prints a PASS or FAIL line per check, exits non zero if
anything fails.

The real cost of that choice: every run needs network access and YouTube being up,
and there's no fast offline loop for iterating on unrelated code. Accepted deliberately
rather than fixed - an offline/mocked layer wouldn't have caught anything this pass
found, and network-dependent tests are the right shape for a tool whose entire job is
talking to a real external service.

## Notes and limits

**Upload dates.** A flat channel listing does not carry upload dates, which is the
tradeoff that makes it fast. So by default the server fills them in from the channel's
public RSS feed, which is one extra cheap request and covers roughly the 15 most
recent videos. Older videos come back with `upload_date` of `null`. Set
`resolve_all_dates=True` to date the whole list, but that costs about one request per
video, so keep `max_results` small when you do.

**Total video count.** `total_videos` is whatever YouTube reports and is often `null`.
The `count` field is always accurate for what was returned.

**Videos with no captions.** Some videos genuinely have no caption track, not even an
auto generated one. The server says so explicitly rather than returning an empty
string, so there is no point retrying those.

**If YouTube asks for a sign in.** YouTube sometimes demands a signed in session and
yt-dlp will report "Sign in to confirm you're not a bot". Tested against 90 real,
distinct videos at meaningful concurrency (10 parallel workers) on 2026-08-08 without
ever triggering it - yt-dlp calls YouTube's internal API with client emulation rather
than scraping the rendered page, which is a different code path from what the casual
"browser automation" bot-check usually catches. So the trigger itself is unverified
here; what *is* verified is the fix and a real failure mode in it.

```
YT_DLP_COOKIES_FROM_BROWSER=chrome    # or firefox, edge - see the caveat below
YT_DLP_COOKIEFILE=C:\path\to\cookies.txt
```

**`YT_DLP_COOKIES_FROM_BROWSER` fails outright while that browser is running**,
confirmed on this machine: with Chrome open, yt-dlp cannot copy its cookie database
(`PermissionError`, since Chrome holds an exclusive lock on it) and the read fails
completely - not silently, it raises `CookieLoadError`. Pointing at a browser that
happens to be closed (`edge`, when Edge wasn't running) worked cleanly. Since Chrome
being open is the normal case, not an edge case, **`YT_DLP_COOKIEFILE` is the more
reliable choice** - export cookies.txt with a browser extension once, and it works
regardless of what's running. The server now recognizes this specific failure
(`_friendly_error`'s `CookieLoadError` branch) and returns an actionable message
telling you to close the browser or switch to `YT_DLP_COOKIEFILE`, instead of
crashing with a raw Python traceback the way it did before this was found and fixed.

**Why frames are fetched rather than seeked.** The obvious design is to have ffmpeg
seek the remote stream URL and range request only the bytes it needs. That does not
work. YouTube binds a stream URL to the player client that requested it and refuses
everyone else, so handing the URL to ffmpeg gets HTTP 403 or a stall that never
finishes. yt-dlp's own `download_ranges` hits the same wall, because it shells out to
ffmpeg too. Forcing the `android` client produces a URL ffmpeg can fetch, but that
client only offers 360p, which is too coarse to read a menu label.

What does work is letting yt-dlp fetch the stream itself, since it holds the matching
client session. That is cheap because the stream is video only, with no audio track
requested. Measured on a 31 minute 720p tutorial: **32 MB in 5.7 seconds**, then frames
come off the local file in well under a second each. The file is cached for the life of
the server process, 3 videos maximum, so more frames from the same video are near
instant. A second call on an already fetched video measured 1.3 seconds against 13.1
cold.

**ffmpeg is required for frames only.** The other three tools do not need it. If it is
missing, `get_video_frames` says so and tells you to run `winget install Gyan.FFmpeg`.

**Live streams are rejected before any fetch is attempted.** A currently-live or
not-yet-started broadcast has no end, so "download the stream" never finishes -
confirmed real 2026-08-08 against Lofi Girl's 24/7 stream: it ran for about 90 seconds
before ffmpeg exited with a bare, unhelpful `code 1`. `get_video_frames` now checks
`is_live`/`live_status` first and fails in about 1-2 seconds with a clear reason
instead. A `was_live` video (the broadcast has ended and YouTube published the
replay) works normally - only genuinely open-ended streams are rejected.

**Playlist URLs work end to end**, confirmed against a real 11-video playlist -
listing, ordering, and channel/count metadata all correct. Their videos follow the
same upload-date rule as any other listing (recent ones resolve free via RSS, older
ones need `resolve_all_dates=True`) - there's nothing playlist-specific about it, an
older video is an older video whether reached by channel or by playlist.

**Region-blocked videos: the fallback path is proven, the specific message is not.**
Two real attempts to trigger an actual geo-block both missed: a video documented in
yt-dlp's own issue tracker as geo-restricted is now blocked everywhere by a copyright
claim instead, and BBC's YouTube channel turned out not to be region-locked the way
BBC iPlayer is. What *is* proven is that the general failure path handles it safely -
any `YoutubeDLError` yt-dlp raises comes back as a clean message, never a crash - and
`_friendly_error` has a specific branch for YouTube's documented "not available in
your country" phrasing, matched by text since yt-dlp has no dedicated exception type
for this. That specific branch is unverified against a real occurrence.

**Transient failures on the frame-fetch path are retried automatically.** Only the
large-stream download in `get_video_frames` does a real content fetch (the other three
tools are lightweight metadata/caption calls, and 90 of those in a row - including a
10-worker concurrent burst - produced zero failures). Confirmed real on 2026-08-08: a
4-hour video's stream fetch returned HTTP 403 once, then succeeded seconds later with
nothing else changed - a signed download URL failing in a way only a fresh extraction
clears, not something yt-dlp's own `extractor_retries` covers, since that only retries
metadata calls. `_local_stream` now retries the whole extraction (not just the byte
fetch) up to 3 times with backoff - but skips retrying entirely for failures no retry
could fix (cookie lock, live stream, private/unavailable, region-block), so those
still fail in one attempt, not three.

**Keeping yt-dlp current.** YouTube changes its internals regularly and yt-dlp keeps
up, so if extractions start failing the first thing to try is an upgrade.

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade yt-dlp
```

## Verified

Tested end to end on 2026-08-08 against `@mwganson` and `@MangoJellySolutions`. Both
listed correctly with real dates and durations. A full auto generated transcript came
back from `Tbiu_rMJolk`, and search returned results. Frames were pulled from
`Xybk1EJfwHk` (Reverse Engineering an STL Fan Impeller) at five transcript chosen
moments in 5 seconds, and were legible enough to read the workbench selector, the model
tree, property values and the status bar dimensions.

A real MCP stdio session completed a handshake, listed all four tools, returned live
listing data, and returned mixed text plus image content from `get_video_frames`. All
four of its error paths (bad timestamp, no timestamp given, timestamp past the end of
the video, unavailable video) return proper MCP errors with actionable messages.

**Hardening pass, 2026-08-08.** Four rough edges from the first verification round,
worked through against real content, not synthetic tests - see "Notes and limits"
above for the full detail on each:

| Area | Result |
|---|---|
| Playlist URLs | Confirmed working end to end against a real 11-video playlist |
| Bot-wall trigger | Not reproduced (90 real requests, 10-way concurrent) - the cookie *fix* was tested instead, and found broken while the named browser is running; fixed |
| Live streams | Real hang found (90s, unhelpful error) and fixed (rejected in ~1-2s) |
| Region-locked videos | Not reproduced (2 real attempts) - general failure handling proven safe regardless; the specific message is unverified |
| Multi-hour videos | Confirmed against a 23-hour transcript and a 4-hour frame fetch (including a frame at the 4:00:00 mark) |
| Retry/backoff | Added for the one path proven to need it (large-stream fetch, real transient 403 reproduced and fixed) - not added to the metadata/caption paths, which showed zero failures across 90 real requests |

One bug found *while fixing another*: the live-stream check itself could crash
unhandled in the same cookie-lock scenario, because it was a bare call with no
`except`. Caught by testing the fix against the earlier finding, not by inspection -
fixed in the same pass.
