import pytest

from app.intents.schema import Classification, Intent


@pytest.mark.parametrize(
    "name, expected",
    [
        ("add_item", "add_item"),
        ("ADD_ITEM", "add_item"),
        ("add-item", "add_item"),
        ("/help", "help"),
        ("store", "add_item"),
        ("done", "mark_done"),
        ("find", "search"),
        ("something_new", "unknown"),
        (None, "unknown"),
        (42, "unknown"),
    ],
)
def test_intent_name_normalization(name, expected):
    assert Intent(name=name, confidence=0.9).name == expected


@pytest.mark.parametrize("conf, expected", [(0.7, 0.7), (85, 0.85), (1.5, 1.0), (-1, 0.0), ("0.9", 0.9), ("high", 0.0), (None, 0.0), (500, 1.0)])
def test_confidence_is_clamped(conf, expected):
    assert Intent(name="help", confidence=conf).confidence == pytest.approx(expected)


def test_bad_entities_and_span_are_coerced():
    i = Intent(name="search", confidence=0.9, entities=["not", "a", "dict"], raw_span=None)
    assert i.entities == {} and i.raw_span == ""


def test_classification_top_and_language_default():
    c = Classification(intents=[Intent(name="help", confidence=0.4), Intent(name="search", confidence=0.8)], language=None)
    assert c.top.name == "search"
    assert c.language == "en"
    assert Classification().top is None


def test_settings_strip_inline_comments(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("KL_MODE", "Polling            # polling | webhook")
    monkeypatch.setenv("WEBHOOK_URL", "               # only for webhook")
    monkeypatch.setenv("KL_LOG_LEVEL", "info")
    s = Settings()
    assert s.kl_mode == "polling" and s.webhook_url == "" and s.kl_log_level == "INFO"
    monkeypatch.setenv("WEBHOOK_URL", "# only for webhook")  # docker compose trims leading spaces
    assert Settings().webhook_url == ""


def test_webhook_full_url_appends_path(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "webhook_secret", "abc")
    monkeypatch.setattr(settings, "webhook_url", "https://x.run.app/")
    assert settings.webhook_full_url == "https://x.run.app/webhook/abc"
    monkeypatch.setattr(settings, "webhook_url", "https://x.run.app/webhook/abc")
    assert settings.webhook_full_url == "https://x.run.app/webhook/abc"
    monkeypatch.setattr(settings, "webhook_url", "")
    assert settings.webhook_full_url == ""
