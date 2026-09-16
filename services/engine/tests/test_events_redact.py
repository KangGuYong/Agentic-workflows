from engine.events.redact import MAX_EVENT_PREVIEW_BYTES, MAX_STORED_BYTES, clip_json, redact


def test_values_under_secret_looking_keys_are_replaced():
    value = {"Authorization": "Bearer x", "api_key": "k", "nested": {"password": "p", "keep": 1},
             "list": [{"token": "t"}], "apikey": "a", "keep": "v"}

    assert redact(value) == {
        "Authorization": "[REDACTED]", "api_key": "[REDACTED]",
        "nested": {"password": "[REDACTED]", "keep": 1},
        "list": [{"token": "[REDACTED]"}], "apikey": "[REDACTED]", "keep": "v",
    }


def test_redaction_copies_and_never_shares_containers():
    value = {"a": {"b": [1]}}
    copied = redact(value)
    copied["a"]["b"].append(2)
    assert value == {"a": {"b": [1]}}


def test_clip_json_keeps_small_values_and_replaces_large_ones():
    small, truncated = clip_json({"text": "짧음"}, MAX_STORED_BYTES)
    assert (small, truncated) == ({"text": "짧음"}, False)

    big, truncated = clip_json({"text": "가" * 200_000}, MAX_EVENT_PREVIEW_BYTES)
    assert truncated and set(big) == {"_truncated"} and len(big["_truncated"]) < 2000
