"""Secret values at rest and on the way back out (2b design §4.1).

The crypto tests need nothing; the store tests take `pool` and so are marked integration automatically.
"""
import pytest

from engine.secrets.crypto import SecretCryptoError, open_secret, seal_secret
from engine.secrets.store import PostgresSecretResolver, delete_secret, list_secrets, put_secret

KEY = b"0123456789abcdef0123456789abcdef"


def test_a_sealed_secret_round_trips():
    sealed = seal_secret(KEY, "TOKEN", "hunter2-secret")

    assert open_secret(KEY, "TOKEN", sealed) == "hunter2-secret"


def test_the_ciphertext_does_not_contain_the_plaintext():
    assert b"hunter2-secret" not in seal_secret(KEY, "TOKEN", "hunter2-secret")


def test_two_seals_of_the_same_value_differ():
    """A fresh nonce each time: equal ciphertexts would tell an attacker two secrets are the same."""
    assert seal_secret(KEY, "TOKEN", "same-value") != seal_secret(KEY, "TOKEN", "same-value")


def test_a_ciphertext_moved_to_another_name_will_not_open():
    """The name is the AAD, so a row copied onto a different name is detected."""
    sealed = seal_secret(KEY, "TOKEN", "hunter2-secret")

    with pytest.raises(SecretCryptoError):
        open_secret(KEY, "OTHER", sealed)


def test_a_tampered_ciphertext_will_not_open():
    sealed = bytearray(seal_secret(KEY, "TOKEN", "hunter2-secret"))
    sealed[-1] ^= 0xFF

    with pytest.raises(SecretCryptoError):
        open_secret(KEY, "TOKEN", bytes(sealed))


def test_a_truncated_ciphertext_will_not_open():
    """Shorter than nonce+tag: it must fail the same way, not raise a slicing error out of the codec."""
    with pytest.raises(SecretCryptoError):
        open_secret(KEY, "TOKEN", b"short")


def test_a_wrong_key_will_not_open():
    sealed = seal_secret(KEY, "TOKEN", "hunter2-secret")

    with pytest.raises(SecretCryptoError):
        open_secret(b"f" * 32, "TOKEN", sealed)


def test_the_error_never_says_which_check_failed():
    """Wrong key, wrong name and tampering must be indistinguishable to whoever sees the message."""
    sealed = seal_secret(KEY, "TOKEN", "hunter2-secret")
    messages = set()
    for key, name in ((b"f" * 32, "TOKEN"), (KEY, "OTHER")):
        with pytest.raises(SecretCryptoError) as exc:
            open_secret(key, name, sealed)
        messages.add(str(exc.value))

    assert len(messages) == 1
    assert "hunter2-secret" not in messages.pop()


async def test_a_secret_is_stored_encrypted_and_resolved_back(pool):
    async with pool.connection() as conn:
        await put_secret(conn, key=KEY, name="TOKEN", value="hunter2-secret")
        row = await (await conn.execute("SELECT ciphertext FROM secrets WHERE name='TOKEN'")).fetchone()

    assert b"hunter2-secret" not in bytes(row["ciphertext"])
    assert await PostgresSecretResolver(pool, KEY).resolve({"TOKEN"}) == {"TOKEN": "hunter2-secret"}


async def test_putting_a_secret_twice_replaces_it(pool):
    async with pool.connection() as conn:
        await put_secret(conn, key=KEY, name="TOKEN", value="first-value")
        await put_secret(conn, key=KEY, name="TOKEN", value="second-value")

    assert await PostgresSecretResolver(pool, KEY).resolve({"TOKEN"}) == {"TOKEN": "second-value"}


async def test_resolving_a_missing_secret_leaves_it_out(pool):
    async with pool.connection() as conn:
        await put_secret(conn, key=KEY, name="TOKEN", value="hunter2-secret")

    assert await PostgresSecretResolver(pool, KEY).resolve({"TOKEN", "GONE"}) == {"TOKEN": "hunter2-secret"}


async def test_resolving_nothing_asks_the_database_nothing(pool):
    """The common case — a request with no {{secret.…}} in it — must not cost a connection."""
    assert await PostgresSecretResolver(pool, KEY).resolve(set()) == {}


async def test_a_row_sealed_with_another_key_reads_as_missing(pool):
    """A rotated key must not turn into an infrastructure error: the node reports SECRET_NOT_FOUND."""
    async with pool.connection() as conn:
        await put_secret(conn, key=b"f" * 32, name="TOKEN", value="hunter2-secret")

    assert await PostgresSecretResolver(pool, KEY).resolve({"TOKEN"}) == {}


async def test_listing_returns_names_and_times_but_never_values(pool):
    async with pool.connection() as conn:
        await put_secret(conn, key=KEY, name="TOKEN", value="hunter2-secret")
        rows = await list_secrets(conn)

    assert [row["name"] for row in rows] == ["TOKEN"]
    assert "ciphertext" not in rows[0]


async def test_deleting_reports_whether_it_applied(pool):
    async with pool.connection() as conn:
        await put_secret(conn, key=KEY, name="TOKEN", value="hunter2-secret")

        assert await delete_secret(conn, "TOKEN") is True
        assert await delete_secret(conn, "TOKEN") is False
