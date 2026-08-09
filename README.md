# youtube-research-mcp

An MCP server that lets Claude research YouTube channels and read video transcripts,
without a YouTube API key and without rendering a browser.

## Legal status - read this before using it

This talks to YouTube through [yt-dlp](https://github.com/yt-dlp/yt-dlp) rather than
YouTube's official Data API, which is outside what YouTube's Terms of Service
contemplate as sanctioned automated access. That's a real thing to know, not a
formality:

- **`yt-dlp` itself is legal to build on and distribute.** It's public domain
  (Unlicense), survived a 2020 DMCA takedown attempt (GitHub reinstated it after EFF
  intervention, on the grounds it has substantial non-infringing uses), and has
  millions of users. Individual open-source tools built on it, used for personal
  research, have a long track record of being left alone.
- **What has consistently drawn legal action is monetizing or centralizing access to
  YouTube content** - commercial "download as a service" sites get sued and shut
  down repeatedly. This project is released free, MIT-licensed, meant to be cloned
  and run locally by each user against their own network - not offered as a hosted
  service. Reselling access to it, or running it as a shared service many people
  connect to, meaningfully changes that risk picture and isn't something this project
  endorses.
- **This is not legal advice**, and no one associated with this project is
  liable for how you use it - see the [LICENSE](LICENSE)'s warranty disclaimer. If
  you're planning anything beyond personal research use, get real legal review first.
- YouTube's anti-automation measures (PO Tokens, bot-checks, IP-based rate limiting)
  are real, active, and have escalated in 2026 specifically - see "Notes and limits"
  below. This tool's reliability is inherently coupled to `yt-dlp`'s ability to keep
  up; periodic maintenance (`pip install --upgrade yt-dlp`) is normal, not a sign
  something is broken.

## How it works

Uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) to talk to YouTube's own internal
JSON endpoints directly, rather than rendering a page in a browser or calling the
official Data API - no API key needed. Everything is read only. Video listings come
from a flat playlist extraction (titles and IDs only, no video data), and transcripts
are read straight from the caption track into memory. Only `get_video_frames` touches
disk, and only a temp file it deletes when the server exits.

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
cannot disturb any other MCP server's dependencies. You'll also need
[ffmpeg](https://ffmpeg.org/download.html) on `PATH` for `get_video_frames` — the
other three tools don't need it.

**Windows (PowerShell):**

```powershell
cd path\to\youtube-research-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
winget install Gyan.FFmpeg   # if ffmpeg isn't already on PATH
```

**macOS / Linux:**

```bash
cd path/to/youtube-research-mcp
python3 -m venv .venv
./.venv/bin/python -m pip install .
brew install ffmpeg   # or apt-get install ffmpeg / your distro's package manager
```

That installs a `yt-research-mcp` console command into the venv - register it with
Claude Code (user scope, so it is available in every project), replacing `path/to`
with wherever you actually cloned this:

```powershell
claude mcp add youtube-research --scope user -- `
  path\to\youtube-research-mcp\.venv\Scripts\yt-research-mcp.exe
```

```bash
claude mcp add youtube-research --scope user -- \
  path/to/youtube-research-mcp/.venv/bin/yt-research-mcp
```

Or add it by hand to `~/.claude.json` (Windows path shown; use forward slashes on
macOS/Linux and drop the `.exe`):

```json
"youtube-research": {
  "type": "stdio",
  "command": "C:\\path\\to\\youtube-research-mcp\\.venv\\Scripts\\yt-research-mcp.exe"
}
```

## Testing

Editable install first, so test changes to the code without reinstalling:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .     # Windows, once
.\.venv\Scripts\python.exe test_smoke.py
```
```bash
./.venv/bin/python -m pip install -e .              # macOS/Linux, once
./.venv/bin/python test_smoke.py
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
YT_DLP_COOKIEFILE=/path/to/cookies.txt
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
come off the local file in well under a second each. Both the file *and* its metadata
are cached for the life of the server process (3 videos maximum), so a second call on
an already-fetched video needs no network round trip at all - measured **0.1 seconds
against 10.3 cold** (an earlier version of this cache kept only the file and refetched
metadata on every hit, which still worked but cost about 1.3s per warm call - fixed in
the pre-publish audit).

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

**Transient failures on both fetch paths are retried automatically.** The
large-stream download in `get_video_frames` (the other three tools are lightweight
metadata calls, and 90 of those in a row - including a 10-worker concurrent burst -
produced zero failures during testing). Confirmed real on 2026-08-08: a 4-hour video's
stream fetch returned HTTP 403 once, then succeeded seconds later with nothing else
changed - a signed download URL failing in a way only a fresh extraction clears, not
something yt-dlp's own `extractor_retries` covers, since that only retries metadata
calls. `_local_stream` now retries the whole extraction (not just the byte fetch) up
to 3 times with backoff - but skips retrying entirely for failures no retry could fix
(cookie lock, live stream, private/unavailable, region-block), so those still fail in
one attempt, not three. `get_video_transcript`'s caption-file fetch got the same
retry logic in the pre-publish audit, on the same reasoning (caption URLs are signed
and time-limited the same way stream URLs are) even though no failure was ever
observed there in testing - proactive, not reactive.

**Only youtube.com/youtu.be URLs are ever accepted.** `list_channel_videos` hands
whatever URL it's given to yt-dlp's extractor, which has a generic fallback capable of
fetching arbitrary URLs, not just YouTube's. Every full-URL input is checked against
the actual host before anything else happens with it, and a non-YouTube host raises
`ValueError` immediately. This matters specifically because MCP tools can be called by
an agent acting on content it read elsewhere - without this check, a crafted playlist
URL pointing somewhere else entirely could have made this server issue an outbound
request to an attacker-chosen destination. Added in the pre-publish audit; the video-ID
path (`get_video_transcript`, `get_video_frames`) never had this exposure in the first
place, since it only ever extracts an 11-character ID and always re-embeds it into a
hardcoded `youtube.com` URL, discarding whatever host the input actually had.

**Two smaller pre-publish audit fixes, both defensive rather than reactive to an
observed failure.** The temp directory `get_video_frames` downloads into is now
swept for leftovers from a prior run's unclean exit (a forceful kill doesn't fire
Python's `atexit`, so a long-lived install could otherwise accumulate one stray
directory per crash). And the stream/metadata cache is now protected by a lock -
the MCP stdio transport is normally one request at a time, but nothing in the
protocol guarantees a client won't ever pipeline overlapping tool calls, and the
cache's check-then-act sequence wasn't safe against that without one.

**Keeping yt-dlp current.** YouTube changes its internals regularly and yt-dlp keeps
up, so if extractions start failing the first thing to try is an upgrade.

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade yt-dlp    # Windows
```
```bash
./.venv/bin/python -m pip install --upgrade yt-dlp             # macOS/Linux
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
