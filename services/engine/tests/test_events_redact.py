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


def test_sensitive_headers_are_redacted_by_name():
    from engine.events.redact import redact_headers

    headers = {"Authorization": "Bearer abc", "set-cookie": "s=1", "X-API-Key": "k",
               "Content-Type": "application/json"}

    assert redact_headers(headers) == {"authorization": "[REDACTED]", "set-cookie": "[REDACTED]",
                                       "x-api-key": "[REDACTED]", "content-type": "application/json"}


def test_header_redaction_lowercases_names_and_keeps_order():
    from engine.events.redact import redact_headers

    assert list(redact_headers({"COOKIE": "a", "Accept": "b"})) == ["cookie", "accept"]


def test_every_credential_header_the_client_can_send_is_covered():
    """The list is the whole defence: a name missing from it stores the credential in node_runs."""
    from engine.events.redact import redact_headers

    sensitive = ["Authorization", "Proxy-Authorization", "Cookie", "Set-Cookie", "X-Api-Key",
                 "X-Auth-Token"]
    redacted = redact_headers(dict.fromkeys(sensitive, "credential-value"))

    assert set(redacted.values()) == {"[REDACTED]"}


def test_a_name_that_merely_contains_a_credential_word_is_not_redacted():
    """Name matching is exact, not substring: `x-api-key-hint` is a different header, and blanket
    substring matching would quietly hide ordinary fields from whoever is debugging a run."""
    from engine.events.redact import redact_headers

    assert redact_headers({"X-Cookie-Policy": "strict"}) == {"x-cookie-policy": "strict"}


def test_duplicate_names_differing_only_in_case_collapse_to_one_redaction():
    from engine.events.redact import redact_headers

    assert redact_headers({"Authorization": "a", "authorization": "b"}) == {"authorization": "[REDACTED]"}
