from langgraph.checkpoint.serde.encrypted import EncryptedSerializer


def test_encrypted_serializer_round_trips_with_an_env_key(monkeypatch):
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    serde = EncryptedSerializer.from_pycryptodome_aes()

    typed = serde.dumps_typed({"a": [1, "x"]})

    assert serde.loads_typed(typed) == {"a": [1, "x"]}
    assert b"x" not in typed[1]  # the payload is not stored in the clear


def test_pebble_kills_a_task_that_overruns_its_deadline():
    from concurrent.futures import TimeoutError as FuturesTimeout

    from pebble import ProcessPool

    with ProcessPool(max_workers=1) as pool:
        future = pool.schedule(_spin, args=(30,), timeout=0.5)
        try:
            future.result()
            raise AssertionError("expected a timeout")
        except FuturesTimeout:
            pass
        assert pool.schedule(_spin, args=(0,)).result() == "done"  # the pool still works


def _spin(seconds: float) -> str:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pass
    return "done"
