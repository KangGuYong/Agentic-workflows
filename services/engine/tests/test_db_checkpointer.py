import dataclasses

from engine.compiler.build import compile_workflow
from engine.config import load_config
from engine.db.checkpointer import make_checkpointer
from engine.llm.scripted import ScriptedLLM
from engine.runtime.deps import RunDeps
from engine.runtime.recorder import InMemoryRecorder
from engine.runtime.runner import execute_run

MARKER = "SENSITIVE-OUTPUT-0123456789"

TINY = {
    "version": "1",
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "안녕"}},
        {"id": "end", "type": "end", "config": {"outputs": {"result": "{{ llm_1.text }}"}}},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "llm_1"},
              {"id": "e2", "source": "llm_1", "target": "end"}],
}


async def _blobs(pool) -> list[bytes]:
    async with pool.connection() as conn:
        rows = await (await conn.execute("SELECT blob FROM checkpoint_blobs")).fetchall()
    return [row["blob"] or b"" for row in rows]


async def _run(pool, config, run_id: str, llm) -> tuple:
    compiled = compile_workflow(TINY, checkpointer=make_checkpointer(pool, config))
    deps = RunDeps(run_id=run_id, llm=llm, recorder=InMemoryRecorder())
    outcome = await execute_run(compiled, deps=deps, inputs={})
    return outcome, deps


async def test_node_output_is_not_stored_in_the_clear(pool, db_url):
    config = dataclasses.replace(load_config(), database_url=db_url)
    assert config.encrypt_checkpoints  # the fixture sets LANGGRAPH_AES_KEY

    outcome, _ = await _run(pool, config, "run-encrypted", ScriptedLLM([MARKER]))

    assert (outcome.status, outcome.outputs) == ("succeeded", {"result": MARKER})
    blobs = await _blobs(pool)
    assert blobs and all(MARKER.encode() not in blob for blob in blobs)


async def test_an_encrypted_checkpoint_is_readable_by_the_next_worker(pool, db_url):
    config = dataclasses.replace(load_config(), database_url=db_url)
    await _run(pool, config, "run-resume", ScriptedLLM([MARKER]))

    # a fresh saver and an empty LLM script: anything re-executed would fail
    outcome, deps = await _run(pool, config, "run-resume", ScriptedLLM([]))

    assert (outcome.status, outcome.outputs) == ("succeeded", {"result": MARKER})
    assert deps.recorder.records == []


async def test_development_mode_writes_readable_checkpoints(pool, db_url):
    config = dataclasses.replace(load_config(), database_url=db_url, encrypt_checkpoints=False)

    outcome, _ = await _run(pool, config, "run-plain", ScriptedLLM([MARKER]))

    assert outcome.status == "succeeded"
    blobs = await _blobs(pool)
    assert any(MARKER.encode() in blob for blob in blobs)  # the point of refusing to start without a key
