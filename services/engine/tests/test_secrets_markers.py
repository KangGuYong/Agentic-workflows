"""Per-run secret markers (2b design §4.3): what the renderer substitutes in place of a value."""
import pytest

from engine.secrets.markers import (
    SecretMarkers,
    find_names,
    marker_for,
    nonce_for,
    redact_values,
    substitute,
)

KEY = b"0123456789abcdef0123456789abcdef"
NONCE = nonce_for("run-1", KEY)


def test_the_nonce_is_stable_for_a_run():
    """A replay after a worker restart must render identically, or the record and the checkpoint diverge."""
    assert nonce_for("run-1", KEY) == NONCE
    assert nonce_for("run-2", KEY) != NONCE


def test_the_nonce_depends_on_the_key():
    """Two deployments sharing a run id must not share a marker namespace."""
    assert nonce_for("run-1", b"f" * 32) != NONCE


def test_the_nonce_matches_the_shape_the_marker_pattern_accepts():
    assert find_names(marker_for("API_TOKEN", NONCE), NONCE) == {"API_TOKEN"}


def test_the_marker_mapping_answers_any_valid_name():
    markers = SecretMarkers(NONCE)

    assert markers["API_TOKEN"] == marker_for("API_TOKEN", NONCE)
    with pytest.raises(KeyError):
        markers["not-a-valid-name"]


@pytest.mark.parametrize("name", ["lower", "1LEADING", "WITH-DASH", "A" * 65, "", "WITH SPACE"])
def test_a_name_the_api_would_refuse_is_not_answered(name):
    """The marker namespace and the API's name rule are the same rule; a name the API cannot store must
    not render into a marker that could never resolve."""
    with pytest.raises(KeyError):
        SecretMarkers(NONCE)[name]


def test_names_are_found_only_for_this_runs_nonce():
    text = f"Bearer {marker_for('API_TOKEN', NONCE)} and {marker_for('OTHER', 'f' * 16)}"

    assert find_names(text, NONCE) == {"API_TOKEN"}


def test_a_foreign_marker_does_not_make_the_node_resolve_its_secret():
    """Same name, wrong nonce: the name must not even be looked up, or tenant text could make the node
    fetch a secret the workflow never referenced."""
    assert find_names(marker_for("API_TOKEN", "f" * 16), NONCE) == set()


def test_substitution_replaces_only_this_runs_markers():
    """The foreign marker carries the *same name* on purpose: with a different name the lookup misses
    anyway, so the test would pass even with the nonce check deleted and prove nothing. This is the shape
    that matters — text from another run, or tenant text that guessed a name, must not be filled with
    this run's value."""
    foreign = marker_for("API_TOKEN", "f" * 16)
    text = f"{marker_for('API_TOKEN', NONCE)}|{foreign}"

    assert substitute(text, {"API_TOKEN": "real-value"}, NONCE) == f"real-value|{foreign}"


def test_an_unresolved_marker_is_left_alone():
    text = marker_for("API_TOKEN", NONCE)

    assert substitute(text, {}, NONCE) == text


def test_a_value_that_looks_like_a_marker_is_not_expanded_again():
    """A resolved value is inserted literally: one pass, so a secret whose text happens to contain a
    marker cannot pull in a second secret."""
    nested = marker_for("OTHER", NONCE)
    result = substitute(marker_for("API_TOKEN", NONCE), {"API_TOKEN": nested, "OTHER": "leaked"}, NONCE)

    assert result == nested


def test_redaction_replaces_a_value_anywhere_in_a_string():
    value = {"message": "token=abc12345", "other": "xxabc12345yy", "n": 1}

    assert redact_values(value, ["abc12345"]) == {
        "message": "token=[REDACTED]", "other": "xx[REDACTED]yy", "n": 1}


def test_redaction_reaches_nested_values_and_keys():
    value = {"a": [{"b": "see abc12345"}], "abc12345": "x"}

    assert redact_values(value, ["abc12345"]) == {"a": [{"b": "see [REDACTED]"}], "[REDACTED]": "x"}


def test_overlapping_secrets_are_replaced_longest_first():
    """Shortest-first would cut the long one in half and leave the rest of it exposed."""
    assert redact_values("abc12345678", ["abc12345678", "abc12345"]) == "[REDACTED]"


def test_every_occurrence_is_replaced():
    assert redact_values("a abc12345 b abc12345", ["abc12345"]) == "a [REDACTED] b [REDACTED]"


def test_redaction_leaves_non_string_leaves_alone():
    value = {"n": 1, "f": 1.5, "b": True, "nil": None}

    assert redact_values(value, ["abc12345"]) == value


def test_redacting_nothing_is_a_no_op():
    value = {"message": "token=abc12345"}

    assert redact_values(value, []) == value
    assert redact_values(value, [""]) == value
