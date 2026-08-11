"""Offline tests for get_video_frames' output_dir / include_images additions.

Run:  .venv\\Scripts\\python.exe test_frame_saving.py

No network and no ffmpeg required: server._local_stream (the video fetch) and
server._grab_frame (the ffmpeg call) are both faked, the same way _run_search
and _fetch_rss_bytes were faked for the other two features added today. What's
under test here is the disk-writing logic itself - filenames, overwrite
behaviour, per-frame failure isolation, and the two new argument-validation
rules - not yt-dlp or ffmpeg, which the live suite already covers.

Same PASS/FAIL harness and exit code convention as the other test_*.py files.
"""

import os
import shutil
import sys
import tempfile
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

FAKE_JPEG = b"\xff\xd8" + b"fake-frame-bytes" + b"\xff\xd9"


def fake_local_stream(video_id, max_height):
    return "C:/fake/path/video.mp4", {"title": "Fake Tutorial", "duration": 600}


def fake_grab_frame_ok(args):
    path, seconds, width, quality = args
    return seconds, FAKE_JPEG, None


def fake_grab_frame_one_fails(fail_at):
    def _inner(args):
        path, seconds, width, quality = args
        if seconds == fail_at:
            return seconds, None, "simulated ffmpeg failure"
        return seconds, FAKE_JPEG, None
    return _inner


class TempDir:
    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="yt-research-test-")
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)


def with_fakes(local_stream, grab_frame, ffmpeg_present, fn):
    orig_local_stream = server._local_stream
    orig_grab_frame = server._grab_frame
    orig_ffmpeg = server._ffmpeg
    server._local_stream = local_stream
    server._grab_frame = grab_frame
    server._ffmpeg = (lambda: "ffmpeg") if ffmpeg_present else server._ffmpeg
    try:
        return fn()
    finally:
        server._local_stream = orig_local_stream
        server._grab_frame = orig_grab_frame
        server._ffmpeg = orig_ffmpeg


def call(output_dir=None, include_images=True, timestamps=None):
    return with_fakes(
        fake_local_stream,
        fake_grab_frame_ok,
        True,
        lambda: server.get_video_frames(
            "Tbiu_rMJolk",
            timestamps=timestamps or ["1:00", "2:00"],
            output_dir=output_dir,
            include_images=include_images,
        ),
    )


# --------------------------------------------------------------------------
# Filename / write primitives
# --------------------------------------------------------------------------

def test_frame_filename_is_windows_safe_and_sortable():
    assert server._frame_filename("Tbiu_rMJolk", 4 * 60 + 12) == "Tbiu_rMJolk_00-04-12.jpg"
    assert server._frame_filename("Tbiu_rMJolk", 3725) == "Tbiu_rMJolk_01-02-05.jpg"
    name = server._frame_filename("Tbiu_rMJolk", 90)
    assert ":" not in name, "colons are illegal in Windows filenames"
    # Zero-padding must keep a directory listing in chronological order.
    assert server._frame_filename("v", 9 * 60) < server._frame_filename("v", 61 * 60)


def test_save_frame_writes_real_bytes():
    with TempDir() as d:
        path, err = server._save_frame(d, "Tbiu_rMJolk", 72, FAKE_JPEG)
        assert err is None, err
        assert os.path.exists(path)
        with open(path, "rb") as f:
            assert f.read() == FAKE_JPEG


def test_save_frame_reports_error_without_raising():
    path, err = server._save_frame(
        "Z:/this/drive/does/not/exist/at/all", "Tbiu_rMJolk", 5, FAKE_JPEG)
    assert err is not None, "a bad path must report an error, not silently succeed"


# --------------------------------------------------------------------------
# REQUIRED-shaped: end-to-end save via get_video_frames, no separate download
# --------------------------------------------------------------------------

def test_frames_are_saved_to_output_dir():
    with TempDir() as d:
        out = call(output_dir=d)
        files = sorted(os.listdir(d))
        assert len(files) == 2, files
        for f in files:
            assert f.startswith("Tbiu_rMJolk_") and f.endswith(".jpg"), f
            with open(os.path.join(d, f), "rb") as fh:
                assert fh.read() == FAKE_JPEG
        # Still returns inline images by default - saving is additive, not a
        # replacement for the existing contract.
        summary, images = out[0], out[1:]
        assert len(images) == 2
        assert d in summary or os.path.abspath(d) in summary
        assert "Saved 2 frame" in summary


def test_output_dir_is_created_if_missing():
    with TempDir() as parent:
        target = os.path.join(parent, "nested", "does", "not", "exist", "yet")
        call(output_dir=target)
        assert os.path.isdir(target)
        assert len(os.listdir(target)) == 2


def test_include_images_false_skips_inline_but_still_saves():
    with TempDir() as d:
        out = call(output_dir=d, include_images=False)
        summary, images = out[0], out[1:]
        assert images == [], "include_images=False must return zero inline images"
        assert len(os.listdir(d)) == 2, "files must still be saved"
        assert "Saved 2 frame" in summary


def test_default_behaviour_is_unchanged_with_no_output_dir():
    """The old contract - inline images only, nothing touches disk - must be
    exactly preserved when output_dir is not given.
    """
    out = call(output_dir=None)
    summary, images = out[0], out[1:]
    assert len(images) == 2
    assert "Saved" not in summary
    assert "->" not in summary, "no save arrow should appear when nothing was saved"


def test_saving_the_same_timestamp_again_overwrites_not_duplicates():
    with TempDir() as d:
        call(output_dir=d, timestamps=["1:00"])
        call(output_dir=d, timestamps=["1:00"])
        files = os.listdir(d)
        assert len(files) == 1, f"expected exactly one file after two saves of the same moment: {files}"


def test_two_videos_in_one_output_dir_do_not_collide():
    with TempDir() as d:
        call(output_dir=d, timestamps=["1:00"])
        with_fakes(
            lambda video_id, max_height: ("C:/fake/other.mp4", {"title": "Other", "duration": 600}),
            fake_grab_frame_ok, True,
            lambda: server.get_video_frames("suU74d-lUYM", timestamps=["1:00"], output_dir=d),
        )
        files = os.listdir(d)
        assert len(files) == 2, files
        assert any(f.startswith("Tbiu_rMJolk_") for f in files)
        assert any(f.startswith("suU74d-lUYM_") for f in files)


def test_a_failed_capture_does_not_break_saving_the_rest():
    with TempDir() as d:
        out = with_fakes(
            fake_local_stream, fake_grab_frame_one_fails(120), True,
            lambda: server.get_video_frames(
                "Tbiu_rMJolk", timestamps=["1:00", "2:00", "3:00"], output_dir=d),
        )
        summary = out[0]
        assert len(os.listdir(d)) == 2, "the two good frames must still be saved"
        assert "FAILED" in summary
        assert "Saved 2 frame" in summary


def test_a_failed_disk_write_does_not_lose_other_frames():
    """One frame's write fails (simulated bad path mid-list is not realistic,
    so instead point output_dir somewhere the second write can't land) - must
    not lose frames that saved fine, and must not raise.
    """
    real_save = server._save_frame

    def flaky_save(directory, video_id, seconds, jpeg):
        if seconds == 120:
            return os.path.join(directory, "x.jpg"), "simulated disk full"
        return real_save(directory, video_id, seconds, jpeg)

    with TempDir() as d:
        orig = server._save_frame
        server._save_frame = flaky_save
        try:
            out = call(output_dir=d, timestamps=["1:00", "2:00"])
        finally:
            server._save_frame = orig
        summary = out[0]
        assert len(os.listdir(d)) == 1, "the one good save must survive the other one failing"
        assert "FAILED TO SAVE" in summary
        assert len(out) == 3, "inline images must still both be returned despite the save failure"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def test_include_images_false_without_output_dir_is_rejected():
    try:
        server.get_video_frames("Tbiu_rMJolk", timestamps=["1:00"], include_images=False)
    except ValueError as e:
        assert "output_dir" in str(e)
        return
    raise AssertionError("include_images=False with no output_dir must raise ValueError")


def test_blank_output_dir_is_rejected():
    for blank in ("", "   "):
        try:
            server.get_video_frames("Tbiu_rMJolk", timestamps=["1:00"], output_dir=blank)
        except ValueError:
            continue
        raise AssertionError(f"output_dir={blank!r} should have raised ValueError")


def test_output_dir_none_is_the_default_and_is_fine():
    # No exception, and no disk touched - covered by test_default_behaviour_ too,
    # this just pins the explicit None form.
    call(output_dir=None)


def test_uncreatable_output_dir_raises_runtime_error():
    # A path through an existing FILE (not a directory) can never be mkdir'd into.
    with TempDir() as d:
        blocker = os.path.join(d, "blocker")
        with open(blocker, "w") as f:
            f.write("x")
        target = os.path.join(blocker, "frames")
        try:
            call(output_dir=target)
        except RuntimeError as e:
            assert "output_dir" in str(e)
            return
        raise AssertionError("a path through a file, not a directory, should have raised RuntimeError")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        check(fn.__name__, fn)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        sys.exit(1)
    print(f"all {len(tests)} checks passed")
