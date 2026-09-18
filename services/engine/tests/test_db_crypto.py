"""The two encrypted run payload columns (2b design §2, §9).

`runs.inputs` is the run's initial state, so it cannot be redacted at write — the worker reads it back to
start the graph. Encrypting at rest is what keeps a database dump from handing over every run's input.
"""
import dataclasses

import pytest

from engine.config import load_config
from engine.db import runs as run_db
from engine.db import workflows as workflow_db
from engine.db.crypto import open_payload, seal_payload
from tests.factories import WORKSPACE

SECRETISH = "hunter2-in-the-inputs"
DSL = {"version": "1", "nodes": [], "edges": []}


def _key(db_url: str) -> bytes | None:
    return dataclasses.replace(load_config(), database_url=db_url).secret_key


async def _queued(conn, key, inputs):
    workflow = await workflow_db.create(conn, name="w")
    version = await workflow_db.pin_version(conn, workflow_id=str(workflow["id"]), dsl=DSL, dsl_hash="h")
    return await run_db.insert_queued(
        conn, workflow_id=str(workflow["id"]), version_id=str(version["id"]), workspace_id=WORKSPACE,
        inputs=inputs, idempotency_key=None, store_run_data=True, key=key)


async def test_run_inputs_are_not_stored_in_the_clear(pool, db_url):
    key = _key(db_url)
    async with pool.connection() as conn:
        run = await _queued(conn, key, {"note": SECRETISH})
        raw = await (await conn.execute("SELECT inputs FROM runs WHERE id=%s", (run["id"],))).fetchone()

    assert raw["inputs"] is not None and SECRETISH.encode() not in bytes(raw["inputs"])
    assert run["inputs"] == {"note": SECRETISH}  # the caller still gets the value back


async def test_a_stored_run_decodes_back_to_what_was_written(pool, db_url):
    """`decode_run` is what the worker uses to build the run's initial state; a row read without it is
    ciphertext, and the run would start from nonsense."""
    key = _key(db_url)
    async with pool.connection() as conn:
        run = await _queued(conn, key, {"note": SECRETISH, "n": 1})
        stored = await run_db.get_run(conn, str(run["id"]))

    assert run_db.decode_run(stored, key)["inputs"] == {"note": SECRETISH, "n": 1}


async def test_a_payload_round_trips(pool, db_url):
    """A worker builds the run's initial state from this column: if the decode is not exact the run cannot
    start at all. Round-tripping the awkward values is the cheap version of that check; Task 17 proves it
    through a real worker."""
    key = _key(db_url)
    value = {"한글": "값", "nested": [{"n": 1.5}, None, True], "empty": {}}

    assert open_payload(key, "inputs", seal_payload(key, "inputs", value)) == value
    assert open_payload(key, "inputs", seal_payload(key, "inputs", None)) is None


def test_an_empty_dict_survives_and_does_not_become_null():
    """`{}` and "no inputs at all" are different states; collapsing them would start a run with the wrong
    initial state, and `insert_queued`'s caller cannot tell the difference afterwards."""
    key = b"0" * 32

    assert seal_payload(key, "inputs", {}) is not None
    assert open_payload(key, "inputs", seal_payload(key, "inputs", {})) == {}


async def test_a_payload_cannot_be_moved_between_the_two_columns(pool, db_url):
    """The column name is the AAD, so `outputs` ciphertext pasted into `inputs` reads as unreadable rather
    than as a value the worker would then start a run from."""
    key = _key(db_url)
    if key is None:
        pytest.skip("no key configured: the dev path stores plain JSON on purpose")

    sealed = seal_payload(key, "outputs", {"result": "값"})

    assert open_payload(key, "inputs", sealed) is None


def test_a_row_written_under_another_key_reads_as_unreadable_not_as_an_error():
    """A rotated key must not turn every run detail request into a 500: the run's metadata still
    describes what happened, so the payload alone goes missing."""
    sealed = seal_payload(b"0" * 32, "inputs", {"note": SECRETISH})

    assert open_payload(b"f" * 32, "inputs", sealed) is None


def test_without_a_key_the_value_is_stored_as_plain_json():
    """ENGINE_DEV_INSECURE keeps the development database readable, which is what it already was."""
    stored = seal_payload(None, "inputs", {"note": "x"})

    assert b'"note"' in stored
    assert open_payload(None, "inputs", stored) == {"note": "x"}


def test_a_dev_row_is_not_silently_readable_once_a_key_is_configured():
    """Plain JSON written before a key existed must not decode as if it were sealed."""
    assert open_payload(b"0" * 32, "inputs", seal_payload(None, "inputs", {"note": "x"})) is None
