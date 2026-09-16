import json

from langgraph.checkpoint.memory import InMemorySaver

from engine.compiler.build import compile_workflow
from engine.compiler.state import build_state_type, initial_state, outputs_of
from engine.llm.scripted import ScriptedLLM
from engine.runtime.deps import RunDeps
from engine.runtime.recorder import InMemoryRecorder
from engine.runtime.runner import execute_run


def test_state_type_has_one_channel_per_node():
    fields = build_state_type(["start", "llm_1"]).__annotations__

    assert set(fields) == {"inputs", "routes", "loop_counters", "exec_counts", "out_start", "out_llm_1"}


def test_outputs_are_assembled_from_the_node_channels():
    state = {
        "inputs": {"a": 1},
        "out_start": {"a": 1},
        "out_llm_1": {"text": "답"},
        "out_empty": {},
        "routes": {},
    }

    assert outputs_of(state) == {"start": {"a": 1}, "llm_1": {"text": "답"}, "empty": {}}
    assert outputs_of(initial_state({"a": 1})) == {}


class _SizedSaver(InMemorySaver):
    """Counts the bytes a Postgres checkpointer would write: only channels whose version changed."""

    def __init__(self) -> None:
        super().__init__()
        self.written = 0

    async def aput(self, config, checkpoint, metadata, new_versions):
        values = checkpoint["channel_values"]
        self.written += sum(
            len(json.dumps(values.get(name), default=str, ensure_ascii=False)) for name in new_versions
        )
        return await super().aput(config, checkpoint, metadata, new_versions)


def _chain(length: int) -> dict:
    start_schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    nodes = [{"id": "start", "type": "start", "config": {"inputs": start_schema}}]
    edges = []
    previous = "start"
    for index in range(length):
        node_id = f"template_{index}"
        nodes.append({"id": node_id, "type": "template",
                      "config": {"template": "{{ %s.text }}" % previous if index else "{{ start.text }}"}})
        edges.append({"id": f"e{index}", "source": previous, "target": node_id})
        previous = node_id
    nodes.append({"id": "end", "type": "end", "config": {"outputs": {"final": "{{ %s.text }}" % previous}}})
    edges.append({"id": "e_end", "source": previous, "target": "end"})
    return {"nodes": nodes, "edges": edges}


async def test_checkpoint_writes_do_not_grow_with_the_number_of_steps():
    payload = "가" * 20_000
    saver = _SizedSaver()
    compiled = compile_workflow(_chain(8), checkpointer=saver)
    deps = RunDeps(run_id="run-size", llm=ScriptedLLM([]), recorder=InMemoryRecorder())

    outcome = await execute_run(compiled, deps=deps, inputs={"text": payload})

    assert outcome.status == "succeeded"
    # 10 nodes each storing the payload once, plus inputs: quadratic rewriting would be several times this.
    assert saver.written < 14 * len(payload)
