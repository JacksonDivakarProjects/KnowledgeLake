"""M6 — natural-language message becomes a structured Item."""
import pytest

from app.intents.extractor import ExtractedItem, extract, heuristic_extract


async def test_llm_extraction_uses_quality_model(fake_llm):
    msg = "remind me to email Priya about the Q3 dashboard by Friday"
    fake_llm.extract[msg] = {
        "title": "Email Priya about Q3 dashboard",
        "summary": None,
        "type": "action",
        "tags": ["email", "dashboard"],
        "time_hint": "by Friday",
        "energy": None,
    }
    item, meta = await extract(msg)
    assert item.title == "Email Priya about Q3 dashboard"
    assert item.type == "action"
    assert item.tags == ["email", "dashboard"]
    assert item.time_hint == "by Friday"
    assert meta["provider"] == "groq"
    assert fake_llm.calls[0]["fast"] is False


async def test_llm_output_is_normalized(fake_llm):
    msg = "deep dive postgres mvcc"
    fake_llm.extract[msg] = {
        "title": "  Postgres   MVCC internals. ",
        "summary": "null",
        "type": "Learning",
        "tags": ["Postgres", "#Databases", "postgres", "Data Engineering", "a", "b", "c"],
        "time_hint": "",
        "energy": "HARD",
    }
    item, _ = await extract(msg)
    assert item.title == "Postgres MVCC internals"
    assert item.summary is None
    assert item.type == "topic"
    assert item.tags == ["postgres", "databases", "data-engineering", "a"]
    assert item.time_hint is None
    assert item.energy == "high"


async def test_unknown_type_and_energy_are_coerced(fake_llm):
    msg = "something"
    fake_llm.extract[msg] = {"title": "Something", "type": "banana", "energy": "extreme", "tags": "a, b; c"}
    item, _ = await extract(msg)
    assert item.type == "note"
    assert item.energy is None
    assert item.tags == ["a", "b", "c"]


async def test_wrapped_item_object_is_accepted(fake_llm):
    msg = "learn rust"
    fake_llm.extract[msg] = {"item": {"title": "Rust basics", "type": "topic"}}
    item, _ = await extract(msg)
    assert item.title == "Rust basics"


async def test_llm_missing_time_hint_is_recovered_from_message(fake_llm):
    msg = "quick fix for flaky CI test tomorrow"
    fake_llm.extract[msg] = {"title": "Fix flaky CI test", "type": "action"}
    item, _ = await extract(msg)
    assert item.time_hint == "tomorrow"
    assert item.energy == "low"


@pytest.mark.parametrize("bad", ["not json", "{}", '{"title": ""}', '{"title": null}', "[1,2]"])
async def test_invalid_llm_output_falls_back_to_heuristics(fake_llm, bad):
    msg = "remind me to email Priya by Friday"
    fake_llm.extract[msg] = bad
    item, meta = await extract(msg)
    assert meta["provider"] == "heuristic"
    assert item.title == "Email Priya"
    assert item.type == "action"
    assert item.time_hint == "by Friday"


async def test_llm_outage_falls_back_to_heuristics(fake_llm):
    fake_llm.down = True
    item, meta = await extract("deep dive into kafka consumer groups this weekend")
    assert meta["provider"] == "heuristic"
    assert item.title.lower().startswith("deep dive into kafka")
    assert item.energy == "high"
    assert item.time_hint == "this weekend"
    assert "kafka" in item.tags


async def test_classifier_entities_used_as_hint_when_llm_down(fake_llm):
    fake_llm.down = True
    hint = {"title": "Kafka consumer groups", "type": "topic", "tags": ["kafka"]}
    item, _ = await extract("learn kafka consumer groups", hint=hint)
    assert item.title == "Kafka consumer groups"
    assert item.type == "topic"
    assert item.tags == ["kafka"]


async def test_empty_message_never_crashes(fake_llm):
    item, meta = await extract("")
    assert item.title == "Untitled"
    assert meta["provider"] == "heuristic"
    assert fake_llm.calls == []


def test_heuristic_resource_with_url():
    item = heuristic_extract("https://martinfowler.com/articles/data-mesh.html good read on data mesh")
    assert item.type == "resource"
    assert item.title == "Good read on data mesh"


def test_title_is_capped():
    with pytest.raises(ValueError):
        ExtractedItem(title="   ")
    assert len(ExtractedItem(title="x" * 500).title) == 120
