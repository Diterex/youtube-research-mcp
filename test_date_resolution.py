"""Offline tests for list_channel_videos' upload-date resolution and its
fallback when the RSS feed is unreachable.

Run:  .venv\\Scripts\\python.exe test_date_resolution.py

No network. Pins down the exact failure this suite exists to catch: the RSS
feed (https://www.youtube.com/feeds/videos.xml) has been observed live
returning a bare HTTP 500 on one probe and a 404 on the very next, for the
same channel, with nothing else changed - not a blip to retry through so much
as an unreliable dependency. _rss_dates and _fetch_rss_bytes are split apart
(same pattern as _run_search for search_youtube) specifically so that
unreliability can be faked here instead of waiting for it to happen again live.

Same PASS/FAIL harness and exit code convention as the other test_*.py files.
"""

import sys
import time
import traceback

from yt_research_mcp import server

failures = []


def check(name, fn):
    try:
        fn()
        print(f"PASS  {name}")
    except Exception as e:
        failures.append(name)
        print(f"FAIL  {name}: {type(e).__name__}: {e}")
        traceback.print_exc(limit=4)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

def rss_xml(entries):
    """A minimal real-shaped Atom+yt feed body: [(video_id, 'YYYY-MM-DD'), ...]."""
    atom_uri = server.ATOM_NS[1:-1]
    yt_uri = server.YT_NS[1:-1]
    items = "".join(
        f"<entry><yt:videoId>{vid}</yt:videoId>"
        f"<published>{date}T00:00:00+00:00</published></entry>"
        for vid, date in entries
    )
    return f'<feed xmlns="{atom_uri}" xmlns:yt="{yt_uri}">{items}</feed>'.encode("utf-8")


class FakeRSS:
    """Replaces server._fetch_rss_bytes. fail_times controls how many calls
    raise before (optionally) succeeding; permanent=True raises something
    _is_permanent_failure recognises, so the retry loop must not waste
    attempts on it.
    """

    def __init__(self, xml_entries=None, fail_times=0, permanent=False):
        self.xml_entries = xml_entries or []
        self.fail_times = fail_times
        self.permanent = permanent
        self.calls = 0

    def __call__(self, channel_id):
        self.calls += 1
        if self.permanent or self.calls <= self.fail_times:
            msg = "404 Not Found" if self.permanent else "HTTP Error 500: Internal Server Error"
            raise RuntimeError(msg)
        return rss_xml(self.xml_entries)


def video_entry(video_id, title="Some video"):
    return {"id": video_id, "title": title, "url": None, "duration": 600,
             "view_count": 100}


class FakeExtract:
    """Replaces server._extract. Branches on URL shape: a channel/playlist
    listing vs. a single watch URL (what _one_video_date calls per-video).
    """

    def __init__(self, entries, channel_id="UCFAKE00000000000000000",
                 per_video_dates=None):
        self.entries = entries
        self.channel_id = channel_id
        self.per_video_dates = per_video_dates or {}
        self.per_video_calls = []

    def __call__(self, url, **kwargs):
        if "watch?v=" in url:
            vid = url.rsplit("=", 1)[-1]
            self.per_video_calls.append(vid)
            date = self.per_video_dates.get(vid)
            return {"upload_date": date}
        return {
            "entries": self.entries,
            "channel_id": self.channel_id,
            "channel": "Fake Channel",
            "playlist_count": len(self.entries),
        }


def with_fakes(extract, rss, fn, sleep=True):
    orig_extract, orig_rss = server._extract, server._fetch_rss_bytes
    orig_sleep = time.sleep
    server._extract, server._fetch_rss_bytes = extract, rss
    if not sleep:
        server.time.sleep = lambda *a, **k: None
    try:
        return fn()
    finally:
        server._extract, server._fetch_rss_bytes = orig_extract, orig_rss
        server.time.sleep = orig_sleep


# --------------------------------------------------------------------------
# _rss_dates: retry and permanent-vs-transient behaviour, in isolation
# --------------------------------------------------------------------------

def test_rss_dates_succeeds_on_first_try():
    rss = FakeRSS(xml_entries=[("v1", "2024-01-01"), ("v2", "2024-01-02")])
    dates, ok = with_fakes(None, rss, lambda: server._rss_dates("UCxxx"), sleep=False)
    assert ok is True
    assert dates == {"v1": "2024-01-01", "v2": "2024-01-02"}
    assert rss.calls == 1


def test_rss_dates_recovers_within_retry_budget():
    """The actual common case: a transient hiccup that a retry alone fixes,
    with no need for the per-video fallback at all.
    """
    rss = FakeRSS(xml_entries=[("v1", "2024-01-01")], fail_times=1)
    dates, ok = with_fakes(None, rss, lambda: server._rss_dates("UCxxx"), sleep=False)
    assert ok is True, "a transient failure that clears within budget must not be reported as down"
    assert dates == {"v1": "2024-01-01"}
    assert rss.calls == 2


def test_rss_dates_retries_transient_failures_up_to_the_budget():
    rss = FakeRSS(fail_times=99)  # never succeeds, transient-looking every time
    dates, ok = with_fakes(None, rss, lambda: server._rss_dates("UCxxx"), sleep=False)
    assert ok is False
    assert dates == {}
    assert rss.calls == 3, f"expected 3 attempts for a transient-looking failure, got {rss.calls}"


def test_rss_dates_does_not_waste_retries_on_permanent_failures():
    rss = FakeRSS(permanent=True)
    dates, ok = with_fakes(None, rss, lambda: server._rss_dates("UCxxx"), sleep=False)
    assert ok is False
    assert rss.calls == 1, f"a permanent-looking failure should fail fast, got {rss.calls} attempts"


def test_rss_dates_treats_unparseable_body_as_feed_down():
    """An error interstitial served with a 200 status parses as invalid XML -
    this must be indistinguishable from a hard network failure to the caller.
    """
    def bad_xml(channel_id):
        return b"<html><body>Something went wrong</body>"  # malformed on purpose
    dates, ok = with_fakes(None, bad_xml, lambda: server._rss_dates("UCxxx"), sleep=False)
    assert ok is False
    assert dates == {}


def test_rss_dates_no_channel_id_is_not_a_failure():
    dates, ok = server._rss_dates(None)
    assert ok is True and dates == {}


# --------------------------------------------------------------------------
# REQUIRED: mock the RSS path failing, confirm the fallback kicks in
# --------------------------------------------------------------------------

def test_list_channel_videos_falls_back_when_rss_is_down():
    entries = [video_entry(f"v{i}") for i in range(5)]
    extract = FakeExtract(entries, per_video_dates={f"v{i}": f"2024-01-0{i+1}" for i in range(5)})
    rss = FakeRSS(permanent=True)  # RSS hard down, as actually observed live

    r = with_fakes(extract, rss, lambda: server.list_channel_videos("@fake", max_results=5), sleep=False)

    assert r["count"] == 5
    dated = [v for v in r["videos"] if v["upload_date"]]
    assert len(dated) == 5, f"fallback should have dated every video here: {r['videos']}"
    assert extract.per_video_calls, "fallback must have made per-video calls"
    assert r["note"], "a degraded path must say so"
    assert "RSS" in r["note"]


def test_list_channel_videos_never_returns_all_none_when_rss_is_down():
    """The exact regression this suite exists to catch: RSS down used to mean
    every single video came back with upload_date=None and no explanation.
    """
    entries = [video_entry(f"v{i}") for i in range(3)]
    extract = FakeExtract(entries, per_video_dates={"v0": "2024-05-01", "v1": "2024-05-02", "v2": "2024-05-03"})
    rss = FakeRSS(fail_times=99)  # exhausts retries, feed_ok False

    r = with_fakes(extract, rss, lambda: server.list_channel_videos("@fake", max_results=3), sleep=False)
    assert any(v["upload_date"] for v in r["videos"]), "regressed: RSS outage silently zeroed every date again"


def test_healthy_rss_needs_no_fallback():
    """Guard against an over-eager fallback: when the feed genuinely works
    (even if it just doesn't cover every video - normal for older uploads),
    no per-video fetch should happen at all.
    """
    entries = [video_entry("v1"), video_entry("v2")]
    extract = FakeExtract(entries)
    rss = FakeRSS(xml_entries=[("v1", "2024-06-01")])  # v2 legitimately absent from the feed

    r = with_fakes(extract, rss, lambda: server.list_channel_videos("@fake", max_results=2), sleep=False)
    assert r["note"] == "", f"a healthy feed with partial coverage is not a failure: {r['note']}"
    assert extract.per_video_calls == [], "fallback fired on a working feed"
    v1 = next(v for v in r["videos"] if v["video_id"] == "v1")
    v2 = next(v for v in r["videos"] if v["video_id"] == "v2")
    assert v1["upload_date"] == "2024-06-01"
    assert v2["upload_date"] is None, "v2 not being in the feed's recent window is expected, not an error"


def test_fallback_is_capped_on_a_large_listing():
    n = server._RSS_FALLBACK_CAP + 15
    entries = [video_entry(f"v{i}") for i in range(n)]
    extract = FakeExtract(entries, per_video_dates={f"v{i}": "2024-01-01" for i in range(n)})
    rss = FakeRSS(permanent=True)

    r = with_fakes(extract, rss, lambda: server.list_channel_videos("@fake", max_results=n), sleep=False)

    assert len(extract.per_video_calls) == server._RSS_FALLBACK_CAP, (
        f"fallback should be capped at {server._RSS_FALLBACK_CAP}, made "
        f"{len(extract.per_video_calls)} per-video calls instead"
    )
    dated = [v for v in r["videos"] if v["upload_date"]]
    assert len(dated) == server._RSS_FALLBACK_CAP, len(dated)
    assert str(server._RSS_FALLBACK_CAP) in r["note"]
    assert "resolve_all_dates" in r["note"], "capped note must point at the escape valve"


def test_resolve_all_dates_lifts_the_cap_even_with_rss_down():
    n = server._RSS_FALLBACK_CAP + 10
    entries = [video_entry(f"v{i}") for i in range(n)]
    extract = FakeExtract(entries, per_video_dates={f"v{i}": "2024-01-01" for i in range(n)})
    rss = FakeRSS(permanent=True)

    r = with_fakes(extract, rss, lambda: server.list_channel_videos(
        "@fake", max_results=n, resolve_all_dates=True), sleep=False)

    dated = [v for v in r["videos"] if v["upload_date"]]
    assert len(dated) == n, f"resolve_all_dates=True must date everything, got {len(dated)}/{n}"


def test_fallback_that_also_fails_still_returns_a_full_listing():
    """Defense in depth has to survive a second-layer failure too: even if the
    per-video fallback itself can't resolve a date, the listing must still
    come back intact rather than raising.
    """
    entries = [video_entry(f"v{i}") for i in range(3)]
    extract = FakeExtract(entries, per_video_dates={})  # per-video fetch "succeeds" but finds no date
    rss = FakeRSS(permanent=True)

    r = with_fakes(extract, rss, lambda: server.list_channel_videos("@fake", max_results=3), sleep=False)
    assert r["count"] == 3
    assert all(v["upload_date"] is None for v in r["videos"])
    assert r["note"], "still worth explaining why dates are missing"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        check(fn.__name__, fn)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        sys.exit(1)
    print(f"all {len(tests)} checks passed")
