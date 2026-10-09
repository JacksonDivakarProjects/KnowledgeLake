"""M5 — Groq classifies, fallback works, clarification policy is sane."""
import pytest

from app.intents import classifier
from app.intents.classifier import actionable, classify
from tests.helpers import intents


async def test_fast_path_skips_llm(fake_llm):
    cls, meta = await classify("show my tags")
    assert [i.name for i in cls.intents] == ["list_tags"]
    assert meta["provider"] == "rules"
    assert fake_llm.calls == []


async def test_llm_classification_uses_fast_model_json_mode(fake_llm):
    msg = "learn SQL window functions"
    fake_llm.classify[msg] = intents(("add_item", 0.93))
    cls, meta = await classify(msg)
    assert [i.name for i in actionable(cls)] == ["add_item"]
    assert not cls.needs_clarification
    assert meta["provider"] == "groq"
    assert fake_llm.calls[0]["fast"] is True and fake_llm.calls[0]["json_mode"] is True


async def test_prompt_contains_message(fake_llm):
    msg = "learn SQL window functions"
    fake_llm.classify[msg] = intents(("add_item", 0.9))
    await classify(msg)
    assert fake_llm.calls[0]["message"] == msg


async def test_llm_outage_falls_back_to_rules(fake_llm):
    fake_llm.down = True
    cls, meta = await classify("learn kafka consumer groups")
    assert meta["provider"] == "fallback"
    assert cls.source == "fallback"
    assert [i.name for i in actionable(cls)] == ["add_item"]


async def test_llm_outage_question_becomes_search(fake_llm):
    fake_llm.down = True
    cls, _ = await classify("how does kafka rebalancing work?")
    assert [i.name for i in actionable(cls)] == ["search"]


@pytest.mark.parametrize("garbage", ["I think this is add_item", "", "{broken json", '{"intents": "nope"}', "[]"])
async def test_unparseable_llm_output_falls_back(fake_llm, garbage):
    msg = "learn kafka consumer groups"
    fake_llm.classify[msg] = garbage
    cls, meta = await classify(msg)
    assert meta["provider"] == "fallback"
    assert [i.name for i in actionable(cls)] == ["add_item"]


async def test_fenced_json_is_accepted(fake_llm):
    msg = "learn kafka consumer groups"
    fake_llm.classify[msg] = '```json\n{"intents":[{"name":"add_item","confidence":0.9}]}\n```'
    cls, meta = await classify(msg)
    assert meta["provider"] == "groq"
    assert actionable(cls)[0].name == "add_item"


async def test_single_intent_shape_is_accepted(fake_llm):
    msg = "learn kafka consumer groups"
    fake_llm.classify[msg] = '{"intent": "add_item", "confidence": 0.9}'
    cls, _ = await classify(msg)
    assert actionable(cls)[0].name == "add_item"


async def test_multi_intent_with_high_confidence_is_not_clarified(fake_llm):
    msg = "done with terraform, what's next?"
    fake_llm.classify[msg] = intents(
        ("mark_done", 0.92, {"target": "terraform"}, "done with terraform"),
        ("retrieve", 0.9, {}, "what's next?"),
    )
    cls, _ = await classify(msg)
    assert not cls.needs_clarification
    assert [i.name for i in actionable(cls)] == ["mark_done", "retrieve"]


async def test_close_mid_confidence_intents_ask_for_clarification(fake_llm):
    msg = "kafka"
    fake_llm.classify[msg] = intents(("add_item", 0.7), ("search", 0.65))
    cls, _ = await classify(msg)
    assert cls.needs_clarification
    assert actionable(cls) == []
    assert "save this as a new item" in cls.clarification_question
    assert "search" in cls.clarification_question


async def test_low_confidence_asks_for_clarification(fake_llm):
    msg = "hmm maybe"
    fake_llm.classify[msg] = intents(("add_item", 0.4))
    cls, _ = await classify(msg)
    assert cls.needs_clarification and cls.clarification_question


async def test_low_secondary_intent_is_dropped_not_clarified(fake_llm):
    msg = "learn kafka"
    fake_llm.classify[msg] = intents(("add_item", 0.75), ("search", 0.3))
    cls, _ = await classify(msg)
    assert not cls.needs_clarification
    assert [i.name for i in actionable(cls)] == ["add_item"]


async def test_model_hedging_overridden_when_top_is_confident(fake_llm):
    msg = "learn kafka"
    fake_llm.classify[msg] = intents(("add_item", 0.95), clarify=True, question="really?")
    cls, _ = await classify(msg)
    assert not cls.needs_clarification and cls.clarification_question is None


async def test_model_requested_clarification_is_respected(fake_llm):
    msg = "the thing"
    fake_llm.classify[msg] = intents(clarify=True, question="Which thing do you mean?")
    cls, _ = await classify(msg)
    assert cls.needs_clarification
    assert cls.clarification_question == "Which thing do you mean?"


async def test_unknown_intent_triggers_clarification(fake_llm):
    msg = "qwpoeiruty"
    fake_llm.classify[msg] = intents(("unknown", 0.9))
    cls, _ = await classify(msg)
    assert cls.needs_clarification
    assert "/help" in cls.clarification_question


async def test_duplicate_intents_are_deduped(fake_llm):
    msg = "learn kafka"
    fake_llm.classify[msg] = intents(("add_item", 0.6), ("add_item", 0.9), ("unknown", 0.95))
    cls, _ = await classify(msg)
    assert [(i.name, i.confidence) for i in cls.intents] == [("add_item", 0.9)]


async def test_invalid_intent_entries_are_dropped(fake_llm):
    msg = "learn kafka"
    fake_llm.classify[msg] = {"intents": [42, None, {"name": "add_item", "confidence": 0.9}]}
    cls, _ = await classify(msg)
    assert [i.name for i in actionable(cls)] == ["add_item"]


async def test_very_long_message_is_truncated(fake_llm):
    long = "learn " + "x " * 5000
    fake_llm.down = True
    cls, _ = await classify(long)
    assert actionable(cls)  # still classified, didn't blow up


async def test_empty_message(fake_llm):
    cls, _ = await classify("   ")
    assert cls.needs_clarification and actionable(cls) == []


async def test_unexpected_exception_from_llm_layer_still_falls_back(fake_llm, monkeypatch):
    async def boom(*a, **k):
        raise ValueError("unexpected")

    monkeypatch.setattr(classifier.llm, "complete", boom)
    cls, meta = await classify("learn kafka consumer groups")
    assert meta["provider"] == "fallback"
    assert actionable(cls)


async def test_mid_confidence_intents_on_distinct_spans_are_multi_intent(fake_llm):
    msg = "add kafka notes, show my tags"
    fake_llm.classify[msg] = intents(
        ("add_item", 0.75, {}, "add kafka notes"),
        ("list_tags", 0.72, {}, "show my tags"),
    )
    cls, _ = await classify(msg)
    assert not cls.needs_clarification
    assert [i.name for i in actionable(cls)] == ["add_item", "list_tags"]


async def test_mid_confidence_intents_on_same_span_are_ambiguous(fake_llm):
    msg = "kafka streams"
    fake_llm.classify[msg] = intents(("add_item", 0.75, {}, "kafka streams"), ("search", 0.72, {}, "kafka streams"))
    cls, _ = await classify(msg)
    assert cls.needs_clarification
