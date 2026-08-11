"""End-to-end smoke test - hits YouTube for real, no mocks.

Run:  .venv\\Scripts\\python.exe test_smoke.py
Needs the package installed first (`pip install -e .` for local dev, or a
plain `pip install .` also works since this test only imports it, never
edits it). It lists two real channels, pulls one real transcript, runs a
search, and checks that the URL parsers handle every shape of input we
expect. Prints a PASS/FAIL line per check and exits non-zero if anything
failed.
"""

import sys
import traceback

from yt_research_mcp import server

CHANNELS = ["https://www.youtube.com/@mwganson/videos",
            "https://www.youtube.com/@MangoJellySolutions/videos"]

failures = []


def check(name, fn):
    try:
        result = fn()
        print(f"PASS  {name}")
        return result
    except Exception as e:
        failures.append(name)
        print(f"FAIL  {name}: {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)
        return None


def test_url_parsers():
    cases = {
        "@mwganson": "https://www.youtube.com/@mwganson/videos",
        "mwganson": "https://www.youtube.com/@mwganson/videos",
        "https://www.youtube.com/@mwganson": "https://www.youtube.com/@mwganson/videos",
        "https://www.youtube.com/@mwganson/videos": "https://www.youtube.com/@mwganson/videos",
        "https://www.youtube.com/@mwganson/featured": "https://www.youtube.com/@mwganson/videos",
        "https://www.youtube.com/@mwganson/shorts": "https://www.youtube.com/@mwganson/shorts",
        "www.youtube.com/@mwganson/streams": "https://www.youtube.com/@mwganson/streams",
        "UCLNPmhURJNIm9wsRunKM8mA": "https://www.youtube.com/channel/UCLNPmhURJNIm9wsRunKM8mA/videos",
    }
    for given, expected in cases.items():
        got = server._channel_url(given)
        assert got == expected, f"{given!r} -> {got!r}, expected {expected!r}"

    vid_cases = [
        "Tbiu_rMJolk",
        "https://www.youtube.com/watch?v=Tbiu_rMJolk",
        "https://www.youtube.com/watch?v=Tbiu_rMJolk&t=42s",
        "https://youtu.be/Tbiu_rMJolk",
        "youtu.be/Tbiu_rMJolk?si=abc",
        "https://www.youtube.com/shorts/Tbiu_rMJolk",
        "https://www.youtube.com/embed/Tbiu_rMJolk",
    ]
    for given in vid_cases:
        got = server._video_id(given)
        assert got == "Tbiu_rMJolk", f"{given!r} -> {got!r}"

    for bad in ["", "not a video", "https://www.youtube.com/@mwganson/videos"]:
        try:
            server._video_id(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have raised ValueError")
    return True


def test_list(channel):
    r = server.list_channel_videos(channel, max_results=5)
    assert r["count"] == 5, r["count"]
    v = r["videos"][0]
    for field in ("video_id", "title", "url", "duration"):
        assert v[field], f"missing {field} in {v}"
    # Must hold even while YouTube's RSS feed is down (confirmed live 2026-08-10:
    # it has returned bare 500s and 404s) - list_channel_videos now falls back to
    # per-video resolution automatically. Offline coverage of the fallback logic
    # itself lives in test_date_resolution.py; this only proves it's wired in.
    assert any(x["upload_date"] for x in r["videos"]), "no upload dates resolved"
    if r["note"]:
        print(f"      note: {r['note']}")
    print(f"      {r['channel']} - {r['total_videos']} videos total")
    for x in r["videos"]:
        print(f"      {x['upload_date']}  {x['duration']:>8}  {x['video_id']}  {x['title'][:60]}")
    return r


def test_transcript(video_id):
    r = server.get_video_transcript(video_id, include_timestamps=True, max_chars=600)
    assert r["transcript"], "empty transcript"
    assert r["transcript_kind"] in ("manual", "automatic")
    print(f"      {r['title']} [{r['duration']}] {r['transcript_kind']}/{r['language']}")
    print("      " + r["transcript"][:400].replace("\n", "\n      "))
    return r


def test_frames(video_id):
    import time
    t0 = time.time()
    out = server.get_video_frames(video_id, timestamps=["1:00", "2:00"], width=1280)
    cold = time.time() - t0
    summary, images = out[0], out[1:]
    assert len(images) == 2, f"expected 2 frames, got {len(images)}"
    assert all(len(i.data) > 5000 for i in images), "a frame came back suspiciously small"
    assert all(i.data[:2] == b"\xff\xd8" for i in images), "not a JPEG"
    print(f"      {summary.splitlines()[0][:78]}")
    print(f"      cold {cold:.1f}s, sizes {[len(i.data)//1024 for i in images]} KB")

    t1 = time.time()
    server.get_video_frames(video_id, timestamps=["3:00"])
    warm = time.time() - t1
    assert warm < cold, f"cache did not help (cold {cold:.1f}s, warm {warm:.1f}s)"
    print(f"      warm {warm:.1f}s (stream cache working)")

    for bad, why in [({"timestamps": ["banana"]}, "bad timestamp"),
                     ({}, "no timestamps given"),
                     ({"timestamps": ["99:00:00"]}, "past end of video")]:
        try:
            server.get_video_frames(video_id, **bad)
        except ValueError:
            continue
        raise AssertionError(f"{why} should have raised ValueError")
    return True


def test_search():
    r = server.search_youtube("FreeCAD assembly workbench tutorial", max_results=3)
    assert r["count"] >= 1
    for field in ("broadened", "queries_run", "note", "low_confidence"):
        assert field in r, f"missing {field}"
    for v in r["videos"]:
        assert "relevance" in v and "found_via" in v, v
        print(f"      {v['relevance']:.2f}  {v['duration']:>8}  {v['channel']}  {v['title'][:52]}")
    return r


def test_search_broadening_live():
    """The real thing this feature was built for, against live YouTube: an
    academically-phrased query that practitioners never use, plus the blunt
    phrasings a calling LLM would supply. Offline determinism for the decision
    logic lives in test_search_broadening.py; this one only proves the wiring
    works end to end against YouTube's actual ranking.

    Deliberately tolerant: YouTube's index changes, so this asserts that the
    call succeeds, reports honestly, and does not come back empty - not that a
    specific video is found.
    """
    q = "additive manufacturing parameter optimisation for viscoelastic ceramic feedstock"
    r = server.search_youtube(
        q,
        max_results=8,
        broader_terms=["clay 3d printing", "ceramic paste extruder"],
    )
    print(f"      narrow pass: {r['queries_run'][0]['results']} results, broadened={r['broadened']}")
    for step in r["queries_run"][1:]:
        print(f"      + [{step['kind']}] '{step['query'][:52]}' -> {step['results']}")
    assert r["count"] > 0, f"came back empty even after broadening. note: {r['note']}"
    for v in r["videos"][:5]:
        print(f"      {v['relevance']:.2f}  via {str(v['found_via'])[:24]:<24}  {v['title'][:46]}")
    if r["broadened"]:
        assert r["queries_run"][0]["kind"] == "narrow"
        assert len(r["queries_run"]) > 1, "broadened=True but only one query ran"
        print(f"      dropped {r['dropped_as_off_topic']} as off-topic; "
              f"low_confidence={r['low_confidence']}")
        if r["suggested_channels"]:
            print(f"      suggested channels: "
                  f"{', '.join(c['channel'] for c in r['suggested_channels'])}")
    else:
        print("      narrow pass was already healthy - no widening needed (valid outcome)")
    return r


def test_search_exact_mode_makes_no_extra_requests():
    """auto_broaden=False has to behave exactly as the tool did before this
    feature - one query, no widening, whatever YouTube says.
    """
    r = server.search_youtube("viscoelastic feedstock rheology characterisation",
                              max_results=5, auto_broaden=False)
    assert r["broadened"] is False
    assert len(r["queries_run"]) == 1, r["queries_run"]
    print(f"      {r['count']} results, single query, note: {r['note'][:70]}")
    return r


def test_live_stream_guard():
    """get_video_frames must reject a live broadcast in ~seconds, not attempt to
    download an open-ended stream (confirmed real 2026-08-08: that took ~90s and
    ended in an unhelpful bare 'ffmpeg exited with code 1'). Uses whatever is
    actually live on a channel that's reliably streaming 24/7, rather than a
    hardcoded video ID, since a specific stream's ID doesn't stay live forever.
    """
    import time
    live = server.list_channel_videos("https://www.youtube.com/@LofiGirl/streams", max_results=1)
    if not live["videos"]:
        print("      no live video listed right now - skipping (not a failure, just nothing to test against)")
        return True
    vid = live["videos"][0]["video_id"]
    t0 = time.time()
    try:
        server.get_video_frames(vid, timestamps=["0:05"])
        raise AssertionError(f"{vid} is listed live but get_video_frames did not reject it")
    except RuntimeError as e:
        elapsed = time.time() - t0
        assert "live" in str(e).lower(), f"wrong rejection reason: {e}"
        assert elapsed < 15, f"live-stream guard took {elapsed:.1f}s - should fail in ~1-2s, not attempt a download"
        print(f"      rejected {vid} as live in {elapsed:.1f}s: {str(e)[:100]}")
    return True


if __name__ == "__main__":
    check("url parsers", test_url_parsers)
    listings = [check(f"list_channel_videos {c}", lambda c=c: test_list(c)) for c in CHANNELS]
    first = next((l["videos"][0]["video_id"] for l in listings if l), None)
    if first:
        check(f"get_video_transcript {first}", lambda: test_transcript(first))
        check(f"get_video_frames {first}", lambda: test_frames(first))
    check("search_youtube", test_search)
    check("search_youtube broadening (live)", test_search_broadening_live)
    check("search_youtube auto_broaden=False", test_search_exact_mode_makes_no_extra_requests)
    check("live stream guard", test_live_stream_guard)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        sys.exit(1)
    print("all checks passed")
