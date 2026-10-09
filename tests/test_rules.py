import pytest

from app.intents import rules


@pytest.mark.parametrize(
    "message, intent, entities",
    [
        ("help", "help", {}),
        ("/help", "help", {}),
        ("What can you do?", "help", {}),
        ("show my tags", "list_tags", {}),
        ("what tags do I have", "list_tags", {}),
        ("show my queue", "view_queue", {}),
        ("show me everything", "view_queue", {}),
        ("list my tasks", "view_queue", {}),
        ("what's in my backlog", "view_queue", {}),
        ("What should I focus on?", "retrieve", {}),
        ("what should i work on next", "retrieve", {}),
        ("what's next", "retrieve", {}),
        ("what should I study for GCP", "retrieve", {"query": "gcp"}),
        ("prioritize", "retrieve", {}),
        ("what are my goals", "get_context", {}),
        ("what's my current focus?", "get_context", {}),
        ("what am I focusing on right now", "get_context", {}),
        ("I'm focusing on GCP this month", "update_context", {"field": "current_focus", "value": "GCP"}),
        ("my focus is Data Engineering", "update_context", {"field": "current_focus", "value": "Data Engineering"}),
        ("my goal is pass the AWS SA exam", "update_context", {"field": "goals", "value": "pass the AWS SA exam"}),
        ("add goal: ship side project", "update_context", {"field": "goals", "value": "ship side project"}),
        ("feeling tired today", "update_context", {"field": "energy_pref", "value": "low"}),
        ("my energy is high", "update_context", {"field": "energy_pref", "value": "high"}),
        ("set timezone to Europe/London", "update_context", {"field": "timezone", "value": "Europe/London"}),
        ("done with window functions", "mark_done", {"target": "window functions"}),
        ("finished the dbt model", "mark_done", {"target": "dbt model"}),
        ("#12 done", "mark_done", {"target": "#12"}),
        ("done #7", "mark_done", {"target": "#7"}),
        ("mark kafka basics as done", "mark_done", {"target": "kafka basics"}),
        ("find anything about dbt", "search", {"query": "dbt"}),
        ("search kafka", "search", {"query": "kafka"}),
        ("what do I have on airflow?", "search", {"query": "airflow"}),
        ("do i have anything about terraform", "search", {"query": "terraform"}),
        ("add learn rust", "add_item", {}),
        ("remember to renew passport", "add_item", {}),
        ("todo: fix flaky test", "add_item", {}),
        ("remind me to email Priya by Friday", "add_item", {}),
    ],
)
def test_fast_path_classifies_unambiguous_messages(message, intent, entities):
    cls = rules.fast_path(message)
    assert cls is not None, message
    assert [i.name for i in cls.intents] == [intent]
    assert cls.intents[0].confidence >= 0.9
    assert cls.intents[0].entities == entities
    assert cls.source == "rules"


@pytest.mark.parametrize(
    "message",
    [
        "learn SQL window functions",          # plain statement → let the LLM extract nuance
        "done with x and show my queue",       # multi-intent
        "add kafka; what's next?",             # multi-intent
        "I am done",                           # vague target
        "done with it",                        # vague target
        "I'm feeling sad",                     # not an energy level
        "",
        "   ",
    ],
)
def test_fast_path_defers_to_llm(message):
    assert rules.fast_path(message) is None


@pytest.mark.parametrize(
    "message, intent",
    [
        ("learn SQL window functions", "add_item"),
        ("caching might be our bottleneck", "add_item"),
        ("how does kafka rebalancing work?", "search"),
        ("is there anything on airflow", "search"),
        ("asdfghjkl", "unknown"),
        ("??", "unknown"),
    ],
)
def test_fallback_always_produces_an_intent(message, intent):
    cls = rules.fallback(message)
    assert cls.source == "fallback"
    assert cls.intents[0].name == intent


@pytest.mark.parametrize(
    "message, expected",
    [
        ("done with x and show my queue", [("mark_done", {"target": "x"}), ("view_queue", {})]),
        ("done with terraform, what's next?", [("mark_done", {"target": "terraform"}), ("retrieve", {})]),
        ("add learn go; show my tags", [("add_item", {}), ("list_tags", {})]),
    ],
)
def test_fallback_splits_multi_intent_clauses(message, expected):
    cls = rules.fallback(message)
    assert [(i.name, i.entities) for i in cls.intents] == expected
    assert all(i.raw_span for i in cls.intents)


def test_fallback_does_not_split_plain_conjunctions():
    cls = rules.fallback("learn kafka and rust")
    assert [i.name for i in cls.intents] == ["add_item"]
    assert cls.intents[0].raw_span == "learn kafka and rust"


def test_fallback_empty_message_asks_for_input():
    cls = rules.fallback("   ")
    assert cls.intents == [] and cls.needs_clarification
    assert cls.clarification_question


@pytest.mark.parametrize(
    "message",
    [
        "done with terraform, what's next?",
        "add kafka, show my queue",
        "finished dbt model then show my tags",
    ],
)
def test_comma_or_then_clauses_are_treated_as_multi_intent(message):
    assert rules.fast_path(message) is None
