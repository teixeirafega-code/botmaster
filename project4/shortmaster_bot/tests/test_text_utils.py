from __future__ import annotations

from app.utils.text import extract_json_object, normalize_topic_key, safe_filename


def test_topic_key_is_stable_and_normalized() -> None:
    assert normalize_topic_key("  AI Tools!!! ") == normalize_topic_key("AI tools")


def test_extract_json_from_fenced_response() -> None:
    payload = extract_json_object('```json\n{"title": "ok", "value": 1}\n```')
    assert payload == {"title": "ok", "value": 1}


def test_safe_filename() -> None:
    assert safe_filename("Hello: World!") == "hello-world"
