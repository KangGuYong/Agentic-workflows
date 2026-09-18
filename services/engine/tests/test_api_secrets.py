"""The write-only secrets API (2b design §4.2). No endpoint returns a value, masked or otherwise."""
import logging

import pytest

SECRET = "hunter2-secret-value"


async def test_a_secret_can_be_stored_and_listed_but_never_read_back(api):
    stored = await api.put("/secrets/API_TOKEN", json={"value": SECRET})
    listed = await api.get("/secrets")

    assert stored.status_code == 204
    body = listed.json()
    assert [item["name"] for item in body["secrets"]] == ["API_TOKEN"]
    assert SECRET not in listed.text
    assert "value" not in body["secrets"][0] and "ciphertext" not in body["secrets"][0]


async def test_there_is_no_endpoint_that_returns_a_value(api):
    assert (await api.get("/secrets/API_TOKEN")).status_code in (404, 405)


async def test_replacing_a_secret_keeps_one_row(api):
    await api.put("/secrets/API_TOKEN", json={"value": SECRET})
    await api.put("/secrets/API_TOKEN", json={"value": "another-secret-value"})

    assert len((await api.get("/secrets")).json()["secrets"]) == 1


async def test_a_secret_can_be_deleted(api):
    await api.put("/secrets/API_TOKEN", json={"value": SECRET})

    assert (await api.delete("/secrets/API_TOKEN")).status_code == 204
    assert (await api.delete("/secrets/API_TOKEN")).status_code == 404
    assert (await api.get("/secrets")).json()["secrets"] == []


@pytest.mark.parametrize("name", ["lower", "1LEADING", "WITH-DASH", "A" * 65])
async def test_a_bad_name_is_rejected(api, name):
    assert (await api.put(f"/secrets/{name}", json={"value": SECRET})).status_code in (404, 422)


@pytest.mark.parametrize("value", ["short", "", "x" * 4097])
async def test_a_value_outside_the_length_bounds_is_rejected(api, value):
    response = await api.put("/secrets/API_TOKEN", json={"value": value})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_ERROR"


@pytest.mark.parametrize("value", ["x" * 8, "x" * 4096])
async def test_a_value_on_the_length_bounds_is_accepted(api, value):
    assert (await api.put("/secrets/API_TOKEN", json={"value": value})).status_code == 204


async def test_a_non_string_value_is_rejected(api):
    assert (await api.put("/secrets/API_TOKEN", json={"value": 12345678})).status_code == 422


async def test_a_name_at_the_column_bound_is_accepted(api):
    """64 characters is the longest the regex and the table's CHECK both allow; one more is rejected
    above. The two bounds have to agree or the API hands the database a row it refuses."""
    assert (await api.put(f"/secrets/{'A' * 64}", json={"value": SECRET})).status_code == 204


async def test_the_value_never_appears_in_the_logs(api, caplog):
    with caplog.at_level(logging.DEBUG):
        await api.put("/secrets/API_TOKEN", json={"value": SECRET})
        await api.get("/secrets")

    assert SECRET not in caplog.text


async def test_a_rejected_value_never_appears_in_the_error(api):
    """The 422 must describe the bound, not echo what was sent."""
    response = await api.put("/secrets/API_TOKEN", json={"value": "tiny"})

    assert "tiny" not in response.text
