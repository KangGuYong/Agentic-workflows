"""What `/validate` tells the editor beyond the issue list (3 설계 §4.1).

The editor needs three things per node that it must not compute for itself: which nodes are guaranteed
to have run before this one (autocomplete offers exactly those), what each node's output looks like
(so `{{ llm_1.` can offer `text`), and a branch node's handle names (which depend on its *config*, so
`/node-types` cannot carry them). All three already exist inside `analyze`; before this they simply had
no way out of the API.

A second implementation of the guaranteed-set rule in TypeScript would disagree with the engine on
exactly the graphs that are hard -- loops, merges, branches that rejoin -- and the disagreement would
show up as autocomplete offering a variable the validator then rejects.
"""
import json

from tests.helpers import load_golden

CHAIN = {
    "version": "1",
    "nodes": [
        {"id": "start", "type": "start",
         "config": {"inputs": {"type": "object", "properties": {"topic": {"type": "string"}},
                               "required": ["topic"]}}},
        {"id": "template_1", "type": "template", "config": {"template": "{{ start.topic }}"}},
        {"id": "end", "type": "end", "config": {"outputs": {"result": "{{ template_1.text }}"}}},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "template_1"},
              {"id": "e2", "source": "template_1", "target": "end"}],
}


async def _create(api, name: str = "검증") -> dict:
    response = await api.post("/workflows", json={"name": name})
    assert response.status_code == 201
    return response.json()


async def _validate(api, dsl: dict) -> dict:
    created = await _create(api)
    response = await api.post(f"/workflows/{created['id']}/validate", json={"draftDsl": dsl})
    assert response.status_code == 200, response.text
    return response.json()


async def test_reports_the_nodes_guaranteed_to_have_run_before_each_node(api):
    body = await _validate(api, CHAIN)

    assert body["nodes"]["template_1"]["variables"] == ["start"]
    assert body["nodes"]["end"]["variables"] == ["start", "template_1"]
    assert body["nodes"]["start"]["variables"] == []


async def test_a_branch_that_rejoins_guarantees_only_what_every_path_ran(api):
    """The intersection rule (MVP 설계 4.4). `end` is reachable through three different classifier
    handles, so no branch node is guaranteed -- which is exactly why the editor marks those references
    "기본값 필요" instead of offering them plainly."""
    body = await _validate(api, load_golden("routing"))

    assert body["nodes"]["end"]["variables"] == ["classifier_1", "start"]
    for branch in ("llm_billing", "llm_tech", "template_1"):
        assert branch not in body["nodes"]["end"]["variables"]
    # Each branch itself is still guaranteed everything before the split.
    assert body["nodes"]["llm_billing"]["variables"] == ["classifier_1", "start"]


async def test_a_merge_guarantees_every_branch_that_feeds_it(api):
    """The union rule: a merge is the one join that waits for all of its predecessors."""
    body = await _validate(api, load_golden("parallel"))

    assert body["nodes"]["llm_sum"]["variables"] == ["llm_cons", "llm_pros", "merge_1", "start"]


async def test_variables_are_sorted_so_the_response_is_stable(api):
    """`compute_before` returns frozensets, whose iteration order is not stable between processes. An
    unsorted response would make the editor's autocomplete list reshuffle on every keystroke-triggered
    revalidation, and would make any diff of two responses meaningless."""
    first = await _validate(api, load_golden("parallel"))
    second = await _validate(api, load_golden("parallel"))

    for node_id, node in first["nodes"].items():
        assert node["variables"] == sorted(node["variables"])
        assert node["variables"] == second["nodes"][node_id]["variables"]


async def test_reports_the_handles_a_branch_node_actually_has(api):
    """Handle names come from the node's config, so `/node-types` cannot carry them: a classifier's
    handles are whatever categories the tenant typed."""
    routing = await _validate(api, load_golden("routing"))
    assert routing["nodes"]["classifier_1"]["handles"] == ["billing", "tech", "default"]
    assert routing["nodes"]["llm_billing"]["handles"] == ["out"]

    loop = await _validate(api, load_golden("evaluator_loop"))
    assert loop["nodes"]["condition_1"]["handles"] == ["true", "false"]

    hitl = await _validate(api, load_golden("hitl"))
    approval = next(node_id for node_id, node in hitl["nodes"].items() if node["handles"] == ["approve", "reject"])
    assert approval  # the human_approval node, found by its handles rather than a hard-coded id


async def test_reports_each_node_s_output_schema(api):
    body = await _validate(api, CHAIN)

    assert body["nodes"]["template_1"]["outputSchema"] == {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }


async def test_a_structurally_broken_draft_reports_issues_and_no_nodes(api):
    """`analyze` stops at the first phase with errors, so there is no graph to walk. The editor keeps
    its previous analysis in that case -- losing autocomplete the moment a draft is momentarily broken
    is the opposite of helpful -- so the contract is an empty mapping, not a 500 and not a partial one."""
    broken = {"version": "1", "nodes": [{"id": "start", "type": "nope"}], "edges": []}

    body = await _validate(api, broken)

    assert body["issues"], "a broken draft must still report why"
    assert body["nodes"] == {}


def _untyped_input_compared_numerically() -> dict:
    """An input whose schema declares no type, fed to a numeric operand: the engine cannot prove it is a
    number and cannot prove it is not, so it warns and defers to run time (TYPE_WARNING). A *declared*
    string in the same place is an error instead, which is why this needs the untyped property."""
    return {
        "version": "1",
        "nodes": [
            {"id": "start", "type": "start",
             "config": {"inputs": {"type": "object", "properties": {"n": {}}, "required": ["n"]}}},
            {"id": "condition_1", "type": "condition",
             "config": {"conditions": [{"left": "{{ start.n }}", "op": ">=", "right": "8"}]}},
            {"id": "template_yes", "type": "template", "config": {"template": "yes"}},
            {"id": "template_no", "type": "template", "config": {"template": "no"}},
            {"id": "end", "type": "end", "config": {"outputs": {}}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "condition_1"},
            {"id": "e2", "source": "condition_1", "sourceHandle": "true", "target": "template_yes"},
            {"id": "e3", "source": "condition_1", "sourceHandle": "false", "target": "template_no"},
            {"id": "e4", "source": "template_yes", "target": "end"},
            {"id": "e5", "source": "template_no", "target": "end"},
        ],
    }


async def test_a_draft_with_only_warnings_reports_both(api):
    """A warning does not stop the analysis, so the editor gets the badge *and* the autocomplete data."""
    body = await _validate(api, _untyped_input_compared_numerically())

    assert [issue["severity"] for issue in body["issues"]] == ["warning"], body["issues"]
    assert body["nodes"]


async def test_a_reference_error_still_reports_the_node_map(api):
    """Phase 3 (references and types) does not clear the graph, so the editor keeps autocomplete while
    a reference is half-typed -- which is most of the time it is being typed at all. Only a structural
    or graph error (phase 1-2) blanks the map, and that is what `nodes: {}` above covers."""
    broken = json.loads(json.dumps(CHAIN))
    broken["nodes"][1]["config"]["template"] = "{{ start.nope }}"

    body = await _validate(api, broken)

    assert [issue["code"] for issue in body["issues"]] == ["REF_UNKNOWN_FIELD"], body["issues"]
    assert body["nodes"]["template_1"]["variables"] == ["start"]


def _chain_of(count: int) -> dict:
    nodes: list[dict] = [{"id": "start", "type": "start",
                          "config": {"inputs": {"type": "object", "properties": {}}}}]
    edges: list[dict] = []
    previous = "start"
    for index in range(1, count + 1):
        node_id = f"template_{index}"
        nodes.append({"id": node_id, "type": "template", "config": {"template": "x"}})
        edges.append({"id": f"e{index}", "source": previous, "target": node_id})
        previous = node_id
    nodes.append({"id": "end", "type": "end", "config": {"outputs": {}}})
    edges.append({"id": "e_end", "source": previous, "target": "end"})
    return {"version": "1", "nodes": nodes, "edges": edges}


async def test_the_node_map_is_bounded_by_the_workflow_node_limit(api):
    """The response needs no cap of its own. `structure.MAX_NODES` (100) rejects anything larger in
    phase 1 as a LIMIT_EXCEEDED *error*, which leaves the graph unset and the map empty -- so a
    hand-rolled bound here would be a branch no draft could ever reach.

    (3 설계 §4.1 specified a 200-node cap with a `nodesTruncated` flag. That was written without
    checking MAX_NODES; see the Task 3 note.)
    """
    at_limit = await _validate(api, _chain_of(98))  # 98 + start + end = 100
    over_limit = await _validate(api, _chain_of(99))  # 101

    assert len(at_limit["nodes"]) == 100
    assert at_limit["issues"] == []

    assert [issue["code"] for issue in over_limit["issues"]] == ["LIMIT_EXCEEDED"]
    assert over_limit["nodes"] == {}


async def test_adding_the_node_map_did_not_change_the_issue_list(api):
    """The editor's badges are driven by `issues`; a change there would be a silent regression in every
    existing client and in `test_api_workflows.py`'s expectations."""
    body = await _validate(api, CHAIN)

    assert body["issues"] == []
    assert set(body) == {"issues", "nodes"}


async def test_the_response_is_json_serialisable_as_sent(api):
    """Output schemas come straight out of the node specs; a set or a non-string key there would raise
    inside the response encoder rather than in analyze."""
    body = await _validate(api, load_golden("parallel"))

    json.dumps(body, ensure_ascii=False)
