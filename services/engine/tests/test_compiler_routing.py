import pytest

from engine.compiler.routing import resolve_route
from engine.compiler.state import initial_state, merge_dicts
from engine.dsl.models import Edge

EXIT = Edge(id="exit", source="c", sourceHandle="true", target="end")
BACK = Edge(id="back", source="c", sourceHandle="false", target="gen", maxIterations=2)
LOOP_EDGES = {"true": [EXIT], "false": [BACK]}


def test_merge_dicts_and_initial_state():
    assert merge_dicts({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
    assert merge_dicts(None, {"b": 2}) == {"b": 2}
    assert initial_state({"x": 1}) == {"inputs": {"x": 1}, "outputs": {}, "routes": {}, "loop_counters": {}, "exec_counts": {}}


def test_forward_handle_returns_all_targets_in_declaration_order():
    edges = {"out": [Edge(id="e1", source="s", target="a"), Edge(id="e2", source="s", target="b")]}
    decision = resolve_route("template", "out", edges, frozenset(), {})
    assert decision.targets == ["a", "b"]
    assert decision.counters == {}


def test_loop_is_taken_and_counted():
    decision = resolve_route("condition", "false", LOOP_EDGES, frozenset({"back"}), {})
    assert (decision.handle, decision.targets, decision.counters, decision.loop_exhausted) == ("false", ["gen"], {"back": 1}, False)


def test_exhausted_condition_loop_takes_the_opposite_handle():
    decision = resolve_route("condition", "false", LOOP_EDGES, frozenset({"back"}), {"back": 2})
    assert (decision.handle, decision.targets, decision.counters, decision.loop_exhausted) == ("true", ["end"], {}, True)


def test_exhausted_classifier_loop_takes_default():
    edges = {
        "retry": [Edge(id="back", source="k", sourceHandle="retry", target="gen", maxIterations=1)],
        "default": [Edge(id="d", source="k", sourceHandle="default", target="end")],
    }
    decision = resolve_route("classifier", "retry", edges, frozenset({"back"}), {"back": 1})
    assert (decision.handle, decision.targets) == ("default", ["end"])


def test_unknown_handle_raises():
    with pytest.raises(ValueError):
        resolve_route("classifier", "nope", LOOP_EDGES, frozenset(), {})


def test_exhausted_loop_without_an_exit_handle_raises_value_error():
    edges = {"approve": [Edge(id="back", source="h", sourceHandle="approve", target="gen", maxIterations=1)]}
    with pytest.raises(ValueError):
        resolve_route("human_approval", "approve", edges, frozenset({"back"}), {"back": 1})
