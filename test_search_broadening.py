"""Offline tests for search_youtube's auto-broadening and re-ranking.

Run:  .venv\\Scripts\\python.exe test_search_broadening.py

No network. Every test replaces server._run_search with a canned corpus, which
is the whole reason that function was split out - the interesting behaviour here
is the decision logic (when to widen, what to keep, what to throw away), and
that has to be pinned down deterministically. Live behaviour is covered by
test_smoke.py, which does hit YouTube.

Same PASS/FAIL harness and exit code convention as test_smoke.py.
"""

import sys
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
# Fake search
# --------------------------------------------------------------------------

def vid(video_id, title, channel="Some Channel", views=1000):
    return {
        "video_id": video_id,
        "title": title,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "duration_seconds": 600,
        "duration": "10:00",
        "view_count": views,
        "channel": channel,
        "channel_url": f"https://www.youtube.com/@{channel.replace(' ', '').lower()}",
    }


class FakeSearch:
    """Maps an exact query string to a canned result list. Records call order."""

    def __init__(self, corpus, default=()):
        self.corpus = corpus
        self.default = list(default)
        self.calls = []

    def __call__(self, query, max_results):
        self.calls.append(query)
        return [dict(v) for v in self.corpus.get(query, self.default)][:max_results]


def with_search(fake, fn):
    original = server._run_search
    server._run_search = fake
    try:
        return fn()
    finally:
        server._run_search = original


# --------------------------------------------------------------------------
# Scoring primitives
# --------------------------------------------------------------------------

def test_stemming():
    pairs = [
        ("constraints", "constraint"),
        ("printing", "print"),
        ("printers", "print"),
        ("extruded", "extrud"),
        ("properties", "property"),
    ]
    for word, expected in pairs:
        got = server._stem(word)
        assert got == expected, f"_stem({word!r}) -> {got!r}, expected {expected!r}"
    # What actually matters is that related forms land on the SAME stem, not
    # that the stem is a real word.
    for a, b in [("constraints", "constraint"), ("printing", "print"),
                 ("properties", "property"), ("printers", "printer")]:
        assert server._stem(a) == server._stem(b), f"{a}/{b} failed to unify"
    # Short words must survive intact - chopping 'es' off 'yes' would be worse
    # than not stemming at all.
    for word in ("gas", "ties", "3d", "cad"):
        assert len(server._stem(word)) >= 2, f"_stem({word!r}) chewed it up"


def test_content_terms_drop_glue_not_topic():
    got = server._content_terms("How to fix the layer adhesion of a clay print")
    assert "how" not in got and "the" not in got and "of" not in got and "a" not in got, got
    for keep in ("fix", "layer", "adhesion", "clay", "print"):
        assert keep in got, f"{keep} was dropped from {got}"


def test_relevance_scores_what_it_should():
    groups = server._topic_groups(
        "ceramic paste extrusion layer adhesion",
        ["clay 3d printing"],
    )
    exact = server._relevance(vid("a", "Ceramic paste extrusion: fixing layer adhesion"), groups)
    assert exact > 0.9, exact

    # Matches the caller's alternate phrasing completely, and the original query
    # not at all. This is THE case the group design exists for: one bag of words
    # would have scored it near zero and thrown it away.
    synonym = server._relevance(vid("b", "Clay 3D printing for beginners"), groups)
    assert synonym > 0.9, synonym

    noise = server._relevance(vid("c", "Top 10 Gaming Laptops of 2024"), groups)
    assert noise == 0.0, noise

    partial = server._relevance(vid("d", "Ceramic glaze firing schedules"), groups)
    assert 0.0 < partial < server._BROAD_KEEP_AT, partial


def test_channel_name_counts_as_evidence():
    groups = server._topic_groups("clay 3d printing", None)
    bare = server._relevance(vid("a", "Episode 12", channel="Random Uploads"), groups)
    named = server._relevance(vid("b", "Episode 12", channel="Clay 3D Printing"), groups)
    assert named > bare, (bare, named)


def test_mechanical_variants():
    v = server._mechanical_variants(
        "additive manufacturing parameter optimization for viscoelastic ceramic feedstock"
    )
    assert v, "a long academic query must produce shortenings"
    # 'core': register words gone, everything else kept and in order.
    assert v[0] == "additive manufacturing viscoelastic ceramic feedstock", v[0]
    # 'head' and 'lead' cover both ends, because there is no way to know which
    # end carries the distinctive term without a parser.
    assert "viscoelastic ceramic feedstock" in v, v
    assert "additive manufacturing viscoelastic" in v, v

    # A query with a product name up front is why 'lead' exists.
    v2 = server._mechanical_variants("FreeCAD sketcher constraints fully constrained tutorial")
    assert any(x.startswith("freecad") for x in v2), v2

    # Nothing to shorten -> no mechanical broadening is possible, and the tool
    # must say so rather than pretend.
    assert server._mechanical_variants("clay printing") == []


# --------------------------------------------------------------------------
# Phase 1: when to widen
# --------------------------------------------------------------------------

def test_healthy_query_costs_nothing_extra():
    q = "freecad sketcher constraints tutorial"
    fake = FakeSearch({q: [
        vid("1", "FreeCAD sketcher constraints tutorial"),
        vid("2", "Fully constrained sketches in FreeCAD"),
        vid("3", "FreeCAD constraints explained"),
        vid("4", "Sketcher constraint tutorial for FreeCAD"),
    ]})
    r = with_search(fake, lambda: server.search_youtube(q, broader_terms=["cad sketching"]))
    assert fake.calls == [q], f"widened a healthy query: {fake.calls}"
    assert r["broadened"] is False
    assert r["count"] == 4
    # Untouched YouTube order when nothing was broadened.
    assert [v["video_id"] for v in r["videos"]] == ["1", "2", "3", "4"]


def test_auto_broaden_false_is_the_old_behaviour():
    q = "viscoelastic feedstock rheology"
    fake = FakeSearch({q: []})
    r = with_search(fake, lambda: server.search_youtube(
        q, broader_terms=["clay 3d printing"], auto_broaden=False))
    assert fake.calls == [q], fake.calls
    assert r["broadened"] is False and r["count"] == 0


def test_results_that_are_not_on_topic_also_trigger_widening():
    """Jacob's second trigger: the search returned things, they just are not it."""
    q = "ceramic paste extrusion layer adhesion"
    fake = FakeSearch(
        {
            q: [vid("n1", "Top 10 Gaming Laptops of 2024"),
                vid("n2", "Relaxing piano music"),
                vid("n3", "Unboxing my new phone")],
            "clay 3d printing": [vid("w1", "Clay 3D printing: layer adhesion fixes")],
        },
        default=[],
    )
    r = with_search(fake, lambda: server.search_youtube(q, broader_terms=["clay 3d printing"]))
    assert r["broadened"] is True, "non-empty but irrelevant results must still widen"
    assert "clay 3d printing" in fake.calls, fake.calls
    assert "w1" in [v["video_id"] for v in r["videos"]]


# --------------------------------------------------------------------------
# REQUIRED: a thin narrow query gets rescued by the broad pass
# --------------------------------------------------------------------------

def test_thin_query_is_rescued_by_the_broad_pass():
    q = "additive manufacturing parameter optimization for viscoelastic ceramic feedstock"
    good = [
        vid("g1", "Clay 3D printing: dialling in extrusion settings", "Ceramic 3D", 50000),
        vid("g2", "Clay 3D printing layer height and speed", "Ceramic 3D", 41000),
        vid("g3", "How I set up my clay 3d printer", "Potter Prints", 22000),
    ]
    fake = FakeSearch(
        {
            q: [],                       # the real failure: academic phrasing, nothing back
            "clay 3d printing": good,
            "paste extruder settings": [vid("g4", "Paste extruder settings walkthrough")],
        },
        default=[],
    )
    r = with_search(fake, lambda: server.search_youtube(
        q, broader_terms=["clay 3d printing", "paste extruder settings"]))

    assert r["queries_run"][0]["results"] == 0, "phase 1 was supposed to be empty"
    assert r["broadened"] is True
    assert r["count"] >= 4, f"broad pass failed to rescue: {r['count']} results, note={r['note']}"
    ids = [v["video_id"] for v in r["videos"]]
    for expected in ("g1", "g2", "g3", "g4"):
        assert expected in ids, f"{expected} missing from {ids}"
    assert r["low_confidence"] is False, "these are strong matches, not a last resort"
    # Attribution has to survive, or the caller cannot tell what it is looking at.
    assert all(v["found_via"] != "your query" for v in r["videos"])
    # 'Narrow back in' handle: the channel that scored twice is offered up.
    assert any(c["channel"] == "Ceramic 3D" and c["hits"] >= 2
               for c in r["suggested_channels"]), r["suggested_channels"]


def test_rescue_works_with_no_broader_terms_at_all():
    """Mechanical-only path: no caller help, so shortening is all there is."""
    q = "systematic evaluation of layer adhesion in ceramic paste extrusion"
    fake = FakeSearch(
        {
            q: [],
            # 'core' = register words dropped
            "layer adhesion ceramic paste extrusion": [
                vid("m1", "Ceramic paste extrusion: layer adhesion"),
            ],
        },
        default=[],
    )
    r = with_search(fake, lambda: server.search_youtube(q))
    assert r["broadened"] is True
    assert "m1" in [v["video_id"] for v in r["videos"]], r["note"]
    # And it must tell the caller what it could NOT do.
    assert "broader_terms" in r["note"], r["note"]


# --------------------------------------------------------------------------
# REQUIRED: noise from an over-broad pass is filtered back out
# --------------------------------------------------------------------------

def test_noise_from_the_broad_pass_is_filtered_back_out():
    q = "ceramic paste extrusion layer adhesion"
    on_topic = vid("keep", "Ceramic paste extrusion - curing layer adhesion problems")
    noise = [
        vid("junk1", "Top 10 Gaming Laptops of 2024"),
        vid("junk2", "Relaxing piano music for studying"),
        vid("junk3", "I bought a $5000 espresso machine"),
        vid("junk4", "Minecraft speedrun world record"),
        vid("junk5", "How to change a car tyre"),
    ]
    fake = FakeSearch(
        {
            q: [vid("n1", "Ceramic paste extrusion basics")],   # thin: 1 relevant < 3
            "clay 3d printing": [on_topic] + noise,             # wide pass drags in junk
        },
        default=noise,
    )
    r = with_search(fake, lambda: server.search_youtube(q, broader_terms=["clay 3d printing"]))

    ids = [v["video_id"] for v in r["videos"]]
    assert r["broadened"] is True
    assert "n1" in ids, "the caller's own result must never be discarded"
    assert "keep" in ids, f"on-topic broad hit was wrongly dropped: {ids}"
    for junk in ("junk1", "junk2", "junk3", "junk4", "junk5"):
        assert junk not in ids, f"{junk} survived the re-rank: {ids}"
    assert r["dropped_as_off_topic"] >= 5, r["dropped_as_off_topic"]
    assert r["low_confidence"] is False


def test_broad_results_are_held_to_a_higher_bar_than_the_users_own():
    """A weak result from the caller's own query is kept; the identical-scoring
    result from a query they never typed is not. That asymmetry is deliberate.
    """
    weak_title = "Ceramic glaze firing schedules"          # 1 of 5 topic words
    q = "ceramic paste extrusion layer adhesion"
    fake = FakeSearch(
        {q: [vid("mine", weak_title)],
         "clay 3d printing": [vid("theirs", weak_title)]},
        default=[],
    )
    r = with_search(fake, lambda: server.search_youtube(q, broader_terms=["clay 3d printing"]))
    ids = [v["video_id"] for v in r["videos"]]
    assert "mine" in ids, ids
    assert "theirs" not in ids, ids


def test_ranking_puts_the_best_match_first():
    q = "ceramic paste extrusion layer adhesion"
    fake = FakeSearch(
        {q: [vid("weak", "Ceramic studio tour"),
             vid("mid", "Paste extrusion basics")],
         "clay 3d printing": [
             vid("strong", "Ceramic paste extrusion: layer adhesion, solved")]},
        default=[],
    )
    r = with_search(fake, lambda: server.search_youtube(q, broader_terms=["clay 3d printing"]))
    assert r["videos"][0]["video_id"] == "strong", [
        (v["video_id"], v["relevance"]) for v in r["videos"]]
    scores = [v["relevance"] for v in r["videos"]]
    assert scores == sorted(scores, reverse=True), scores


# --------------------------------------------------------------------------
# Failure and edge behaviour
# --------------------------------------------------------------------------

def test_nothing_relevant_anywhere_returns_flagged_best_effort_not_nothing():
    q = "ceramic paste extrusion layer adhesion"
    fake = FakeSearch(
        {q: []},
        default=[vid("x1", "Ceramic mugs I made this week"),
                 vid("x2", "Gaming laptop review")],
    )
    r = with_search(fake, lambda: server.search_youtube(q, broader_terms=["clay 3d printing"]))
    assert r["count"] > 0, "returning nothing is the failure this feature exists to prevent"
    assert r["low_confidence"] is True
    assert "LOW CONFIDENCE" in r["note"]
    # Best of a bad lot, ranked - the ceramic one beats the laptop one.
    assert r["videos"][0]["video_id"] == "x1", [v["video_id"] for v in r["videos"]]


def test_a_failed_wide_search_does_not_fail_the_call():
    q = "ceramic paste extrusion layer adhesion"

    class Flaky(FakeSearch):
        def __call__(self, query, max_results):
            self.calls.append(query)
            if query == "clay 3d printing":
                raise RuntimeError("simulated network failure")
            return [dict(v) for v in self.corpus.get(query, self.default)][:max_results]

    fake = Flaky(
        {q: [vid("n1", "Ceramic paste extrusion basics")],
         "ceramic paste extrusion layer": [vid("w1", "Ceramic paste extrusion layer adhesion tips")]},
        default=[],
    )
    r = with_search(fake, lambda: server.search_youtube(q, broader_terms=["clay 3d printing"]))
    assert "n1" in [v["video_id"] for v in r["videos"]]
    assert "failed" in r["note"].lower(), r["note"]


def test_phase_1_failure_still_raises():
    def boom(query, max_results):
        raise server.YoutubeDLError("nope")

    try:
        with_search(boom, lambda: server.search_youtube("anything at all here"))
    except RuntimeError:
        return
    raise AssertionError("a failure of the caller's own query must raise")


def test_max_results_is_respected_across_both_passes():
    q = "ceramic paste extrusion layer adhesion"
    wide = [vid(f"w{i}", "Ceramic paste extrusion layer adhesion guide") for i in range(30)]
    fake = FakeSearch({q: [vid("n1", "Ceramic paste extrusion basics")],
                       "clay 3d printing": wide}, default=[])
    r = with_search(fake, lambda: server.search_youtube(
        q, max_results=5, broader_terms=["clay 3d printing"]))
    assert r["count"] == 5, r["count"]
    assert len(r["videos"]) == 5


def test_no_duplicate_videos_across_passes():
    q = "ceramic paste extrusion layer adhesion"
    shared = vid("dup", "Ceramic paste extrusion layer adhesion")
    fake = FakeSearch({q: [shared], "clay 3d printing": [shared, vid("other", "Clay 3d printing tips")]},
                      default=[])
    r = with_search(fake, lambda: server.search_youtube(q, broader_terms=["clay 3d printing"]))
    ids = [v["video_id"] for v in r["videos"]]
    assert ids.count("dup") == 1, ids
    assert next(v for v in r["videos"] if v["video_id"] == "dup")["found_via"] == "your query"


def test_broad_query_budget_is_capped():
    q = "systematic evaluation of layer adhesion in ceramic paste extrusion methodology"
    fake = FakeSearch({q: []}, default=[])
    with_search(fake, lambda: server.search_youtube(
        q, broader_terms=["a b c", "d e f", "g h i", "j k l", "m n o"]))
    assert len(fake.calls) <= 1 + server._MAX_BROAD_QUERIES, fake.calls


def test_empty_query_and_bad_max_results_still_rejected():
    for bad in ("", "   ", None):
        try:
            server.search_youtube(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have raised ValueError")
    for bad in (0, 101, -3):
        try:
            server.search_youtube("clay printing", max_results=bad)
        except ValueError:
            continue
        raise AssertionError(f"max_results={bad} should have raised ValueError")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        check(fn.__name__, fn)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        sys.exit(1)
    print(f"all {len(tests)} checks passed")
