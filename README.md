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

Everything is read only, and nothing is ever saved to disk. Video listings come from
a flat playlist extraction (titles and IDs only, no video data), and transcripts are
read straight from the caption track into memory.

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

This hits YouTube for real, with no mocking. It lists two real channels, pulls a real
transcript, runs a search, and checks every URL shape the parsers are supposed to
handle. It prints a PASS or FAIL line per check and exits non zero if anything fails.

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
yt-dlp will report "Sign in to confirm you're not a bot". The fix is to give it your
browser cookies, using either of these environment variables on the server
registration.

```
YT_DLP_COOKIES_FROM_BROWSER=chrome    # or firefox, edge
YT_DLP_COOKIEFILE=C:\path\to\cookies.txt
```

**Keeping yt-dlp current.** YouTube changes its internals regularly and yt-dlp keeps
up, so if extractions start failing the first thing to try is an upgrade.

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade yt-dlp
```

## Verified

Tested end to end on 2026-08-08 against `@mwganson` and `@MangoJellySolutions`. Both
listed correctly with real dates and durations, a full auto generated transcript came
back from `Tbiu_rMJolk`, search returned results, and a real MCP stdio session
completed a handshake, listed all three tools, and returned live data from a tool
call.
