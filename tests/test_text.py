from datetime import datetime

import pytest

from app.services import text as T


def test_tokens_drop_stopwords_and_urls_and_normalize_plurals():
    toks = T.tokens("Learn the SQL window functions https://example.com/x NOW")
    assert toks == ["sql", "window", "function"]


def test_tokens_keep_tech_terms():
    assert set(T.tokens("c++ and c# and node.js and k8s")) >= {"c++", "c#", "node.js", "k8s"}


def test_overlap_score_exact_prefix_and_empty():
    assert T.overlap_score({"kafka"}, {"kafka", "stream"}) == 1.0
    assert T.overlap_score({"kube"}, {"kubernete"}) == 1.0  # prefix match
    assert T.overlap_score({"kafka", "rust"}, {"kafka"}) == 0.5
    assert T.overlap_score(set(), {"x"}) == 0.0
    assert T.overlap_score({"x"}, set()) == 0.0


@pytest.mark.parametrize(
    "text, hint",
    [
        ("email Priya by Friday", "by Friday"),
        ("finish the report today", "today"),
        ("learn kafka this weekend", "this weekend"),
        ("call mom tomorrow", "tomorrow"),
        ("ship it asap", "asap"),
        ("migrate db in 3 days", "in 3 days"),
        ("submit by 15th oct", "by 15th oct"),
        ("read about rust someday", "someday"),
        ("learn SQL window functions", None),
    ],
)
def test_detect_time_hint(text, hint):
    assert T.detect_time_hint(text) == hint


def test_urgency_ordering():
    monday = datetime(2026, 10, 5)  # a Monday
    u = lambda h: T.urgency(h, monday)  # noqa: E731
    assert u(None) == 0.0
    assert u("today") == 1.0 == u("asap")
    assert u("today") > u("tomorrow") > u("by Wednesday") > u("by Sunday")
    assert u("this weekend") > u("next week")
    assert u("next week") > u("this month") > u("next month") > u("someday")
    assert u("in 2 hours") > u("in 2 weeks")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("learn kafka consumer groups", "topic"),
        ("fix the dbt model", "action"),
        ("remind me to email Priya", "action"),
        ("https://youtu.be/abc great talk", "resource"),
        ("read the book Designing Data-Intensive Applications", "resource"),
        ("caching might be the bottleneck", "note"),
    ],
)
def test_guess_type(text, expected):
    assert T.guess_type(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("quick fix flaky CI test today", "action"),
        ("deep dive postgres MVCC", "topic"),
    ],
)
def test_guess_type_ignores_effort_prefix(text, expected):
    assert T.guess_type(text) == expected


def test_guess_tags_skip_noise_and_keep_raw_words():
    assert T.guess_tags("remind me to email Priya by Friday") == ["priya"]
    assert T.guess_tags("deep dive postgres MVCC") == ["postgres", "mvcc"]
    assert T.guess_tags("learn kafka consumer groups this weekend") == ["kafka", "consumer", "groups"]
    assert T.guess_tags("https://example.com/foo") == []


def test_guess_energy():
    assert T.guess_energy("quick fix for the CI") == "low"
    assert T.guess_energy("deep dive into MVCC") == "high"
    assert T.guess_energy("look at logs") is None


def test_make_title_strips_filler_time_and_urls():
    assert T.make_title("remind me to email Priya about Q3 by Friday") == "Email Priya about Q3"
    assert T.make_title("https://example.com/article") == "https://example.com/article"
    long = T.make_title(" ".join(f"word{i}" for i in range(30)))
    assert len(long.split()) == 10
    assert T.make_title("") == "Untitled"


def test_normalize_tag():
    assert T.normalize_tag("  #Data Engineering ") == "data-engineering"
    assert T.normalize_tag("C++") == "c++"
    assert T.normalize_tag("!!!") == ""


def test_chunk_message_respects_limit():
    assert T.chunk_message("short") == ["short"]
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = T.chunk_message(text, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert "\n".join(chunks) == text
    giant = "y" * 1200
    assert [len(c) for c in T.chunk_message(giant, limit=500)] == [500, 500, 200]
