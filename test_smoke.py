"""End-to-end smoke test - hits YouTube for real, no mocks.

Run:  .venv\\Scripts\\python.exe test_smoke.py
It lists two real channels, pulls one real transcript, runs a search, and checks
that the URL parsers handle every shape of input we expect. Prints a PASS/FAIL
line per check and exits non-zero if anything failed.
"""

import sys
import traceback

import server

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
    assert any(x["upload_date"] for x in r["videos"]), "no upload dates resolved"
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
    for v in r["videos"]:
        print(f"      {v['duration']:>8}  {v['channel']}  {v['title'][:60]}")
    return r


if __name__ == "__main__":
    check("url parsers", test_url_parsers)
    listings = [check(f"list_channel_videos {c}", lambda c=c: test_list(c)) for c in CHANNELS]
    first = next((l["videos"][0]["video_id"] for l in listings if l), None)
    if first:
        check(f"get_video_transcript {first}", lambda: test_transcript(first))
        check(f"get_video_frames {first}", lambda: test_frames(first))
    check("search_youtube", test_search)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        sys.exit(1)
    print("all checks passed")
