from engine.dsl.models import WorkflowDSL
from engine.nodes.registry import default_registry
from engine.validator.graph import build_graph, check_graph
from engine.validator.structure import check_structure

START = {"id": "start", "type": "start"}
END = {"id": "end", "type": "end"}
MERGE = {"id": "merge_1", "type": "merge"}
COND = {"id": "condition_1", "type": "condition", "config": {"conditions": [{"left": "1", "op": "==", "right": "1"}]}}
CLASSIFIER = {
    "id": "classifier_1",
    "type": "classifier",
    "config": {"model": "m", "input": "x", "categories": [{"id": "a", "description": "A"}]},
}


def llm(i: int) -> dict:
    return {"id": f"llm_{i}", "type": "llm", "config": {"model": "m", "prompt": "p"}}


def tpl(i: int) -> dict:
    return {"id": f"template_{i}", "type": "template", "config": {"template": "t"}}


def e(edge_id: str, source: str, target: str, handle: str = "out", max_iterations: int | None = None) -> dict:
    edge = {"id": edge_id, "source": source, "sourceHandle": handle, "target": target}
    if max_iterations is not None:
        edge["maxIterations"] = max_iterations
    return edge


def _analyze(nodes: list[dict], edges: list[dict]):
    dsl = WorkflowDSL.model_validate({"nodes": nodes, "edges": edges})
    issues, parsed = check_structure(dsl, default_registry())
    assert issues == [], issues
    graph = build_graph(dsl, parsed)
    return graph, [issue.code for issue in check_graph(graph)]


def test_linear_graph_is_valid_and_ordered():
    graph, codes = _analyze([START, llm(1), END], [e("e1", "start", "llm_1"), e("e2", "llm_1", "end")])
    assert codes == []
    assert graph.order == ["start", "llm_1", "end"]
    assert graph.back_edges == {}


def test_unreachable_node():
    _, codes = _analyze(
        [START, llm(1), llm(2), END],
        [e("e1", "start", "llm_1"), e("e2", "llm_1", "end"), e("e3", "llm_2", "end")],
    )
    assert "UNREACHABLE_FROM_START" in codes


def test_dead_end_node():
    _, codes = _analyze(
        [START, llm(1), llm(2), END],
        [e("e1", "start", "llm_1"), e("e2", "llm_1", "end"), e("e3", "start", "llm_2")],
    )
    assert "HANDLE_NOT_CONNECTED" in codes
    assert "CANNOT_REACH_END" in codes


def test_valid_evaluator_loop():
    graph, codes = _analyze(
        [START, llm(1), COND, END],
        [e("e1", "start", "llm_1"), e("e2", "llm_1", "condition_1"),
         e("e3", "condition_1", "end", "true"), e("back", "condition_1", "llm_1", "false", 3)],
    )
    assert codes == []
    assert set(graph.back_edges) == {"back"}
    assert [edge.id for edge in graph.incoming["llm_1"]] == ["e1"]


def test_loop_without_limit():
    _, codes = _analyze(
        [START, llm(1), COND, END],
        [e("e1", "start", "llm_1"), e("e2", "llm_1", "condition_1"),
         e("e3", "condition_1", "end", "true"), e("back", "condition_1", "llm_1", "false")],
    )
    assert "BACK_EDGE_NO_LIMIT" in codes


def test_cycle_must_start_at_a_branch_node():
    _, codes = _analyze(
        [START, llm(1), llm(2), END],
        [e("e1", "start", "llm_1"), e("e2", "llm_1", "llm_2"), e("e3", "llm_2", "llm_1"), e("e4", "llm_1", "end")],
    )
    assert "ILLEGAL_CYCLE" in codes


def test_max_iterations_only_on_back_edges():
    _, codes = _analyze([START, llm(1), END], [e("e1", "start", "llm_1", max_iterations=2), e("e2", "llm_1", "end")])
    assert "MAX_ITERATIONS_ON_FORWARD_EDGE" in codes


def test_both_condition_handles_looping_is_a_conflict():
    _, codes = _analyze(
        [START, llm(1), COND, END],
        [e("e1", "start", "llm_1"), e("e2", "llm_1", "condition_1"),
         e("b1", "condition_1", "llm_1", "true", 1), e("b2", "condition_1", "llm_1", "false", 1)],
    )
    assert "BACK_EDGE_HANDLE_CONFLICT" in codes


def test_classifier_default_cannot_loop():
    _, codes = _analyze(
        [START, llm(1), CLASSIFIER, END],
        [e("e1", "start", "llm_1"), e("e2", "llm_1", "classifier_1"),
         e("e3", "classifier_1", "end", "a"), e("back", "classifier_1", "llm_1", "default", 2)],
    )
    assert "BACK_EDGE_HANDLE_CONFLICT" in codes


def test_valid_parallel_region():
    _, codes = _analyze(
        [START, llm(1), llm(2), MERGE, END],
        [e("e1", "start", "llm_1"), e("e2", "start", "llm_2"),
         e("e3", "llm_1", "merge_1"), e("e4", "llm_2", "merge_1"), e("e5", "merge_1", "end")],
    )
    assert codes == []


def test_parallel_branches_must_merge():
    _, codes = _analyze(
        [START, llm(1), llm(2), END],
        [e("e1", "start", "llm_1"), e("e2", "start", "llm_2"), e("e3", "llm_1", "end"), e("e4", "llm_2", "end")],
    )
    assert "INVALID_PARALLEL_REGION" in codes


def test_no_branch_nodes_inside_parallel_region():
    _, codes = _analyze(
        [START, llm(1), COND, MERGE, END],
        [e("e1", "start", "llm_1"), e("e2", "start", "condition_1"),
         e("e3", "llm_1", "merge_1"), e("e4", "condition_1", "merge_1", "true"),
         e("e5", "condition_1", "merge_1", "false"), e("e6", "merge_1", "end")],
    )
    assert "INVALID_PARALLEL_REGION" in codes


def test_parallel_branch_needs_at_least_one_node():
    _, codes = _analyze(
        [START, llm(1), MERGE, END],
        [e("e1", "start", "llm_1"), e("e2", "start", "merge_1"), e("e3", "llm_1", "merge_1"), e("e4", "merge_1", "end")],
    )
    assert "INVALID_PARALLEL_REGION" in codes


def test_merge_without_fan_out():
    _, codes = _analyze(
        [START, llm(1), MERGE, END],
        [e("e1", "start", "llm_1"), e("e2", "llm_1", "merge_1"), e("e3", "merge_1", "end")],
    )
    assert "MERGE_WITHOUT_FAN_OUT" in codes


def test_parallel_region_inside_loop():
    _, codes = _analyze(
        [START, tpl(1), llm(1), llm(2), MERGE, COND, END],
        [e("e1", "start", "template_1"), e("e2", "template_1", "llm_1"), e("e3", "template_1", "llm_2"),
         e("e4", "llm_1", "merge_1"), e("e5", "llm_2", "merge_1"), e("e6", "merge_1", "condition_1"),
         e("e7", "condition_1", "end", "true"), e("back", "condition_1", "template_1", "false", 2)],
    )
    assert "PARALLEL_IN_LOOP" in codes


def test_fan_out_limit():
    nodes = [START, MERGE, END] + [llm(i) for i in range(11)]
    edges = [e(f"a{i}", "start", f"llm_{i}") for i in range(11)]
    edges += [e(f"b{i}", f"llm_{i}", "merge_1") for i in range(11)]
    edges.append(e("c", "merge_1", "end"))
    _, codes = _analyze(nodes, edges)
    assert "LIMIT_EXCEEDED" in codes


def test_no_approval_inside_parallel_region():
    approval = {"id": "human_approval_1", "type": "human_approval", "config": {"message": "m"}}
    _, codes = _analyze(
        [START, llm(1), approval, MERGE, END],
        [e("e1", "start", "llm_1"), e("e2", "start", "human_approval_1"), e("e3", "llm_1", "merge_1"),
         e("e4", "human_approval_1", "merge_1", "approve"), e("e5", "human_approval_1", "end", "reject"),
         e("e6", "merge_1", "end")],
    )
    assert "INVALID_PARALLEL_REGION" in codes


def test_merge_of_exclusive_condition_paths_is_not_a_parallel_region():
    _, codes = _analyze(
        [START, COND, llm(1), llm(2), MERGE, END],
        [e("e1", "start", "condition_1"), e("e2", "condition_1", "llm_1", "true"),
         e("e3", "condition_1", "llm_2", "false"), e("e4", "llm_1", "merge_1"), e("e5", "llm_2", "merge_1"),
         e("e6", "merge_1", "end")],
    )
    assert "MERGE_WITHOUT_FAN_OUT" in codes


def test_merge_takes_only_its_own_region():
    graph, _ = _analyze(
        [START, COND, llm(1), llm(2), MERGE, END],
        [e("e1", "start", "condition_1"), e("e2", "condition_1", "llm_1", "true"),
         e("e3", "condition_1", "llm_2", "true"), e("e4", "llm_1", "merge_1"), e("e5", "llm_2", "merge_1"),
         e("e6", "condition_1", "merge_1", "false"), e("e7", "merge_1", "end")],
    )
    issues = [(issue.code, issue.nodeId) for issue in check_graph(graph)]
    assert issues == [("INVALID_PARALLEL_REGION", "merge_1")]


def test_graph_issues_are_bounded():
    nodes = [START, END] + [llm(i) for i in range(98)]
    _, codes = _analyze(nodes, [e("e1", "start", "end")])
    assert len(codes) == 100
