"""The exact pydantic sentences the editor translates into Korean.

`engine/validator/structure.py::pydantic_issues` sends pydantic's own `msg` to the client, and the
editor (`apps/web/lib/errors/korean.ts`) matches on that text because the text is all that crosses the
wire -- pydantic's error *type* is not transmitted.

That makes a pydantic upgrade able to silently un-translate the editor: nothing in the web tests can
see pydantic. So the strings are pinned here, next to the pydantic that produces them. A failure in
this file means `korean.ts` needs the new wording, not that this file needs updating to match.
"""
from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, Field, ValidationError


class Sample(BaseModel):
    model_config = {"extra": "forbid"}

    text: str = Field(min_length=1, max_length=5)
    count: int = Field(ge=0, le=10)
    ratio: float
    choice: Literal["a", "b"]


def messages(**values: object) -> dict[str, str]:
    with pytest.raises(ValidationError) as caught:
        Sample(**values)  # type: ignore[arg-type]
    return {error["type"]: error["msg"] for error in caught.value.errors()}


def test_the_sentences_the_editor_translates() -> None:
    absent = messages()
    short = messages(text="", count=0, ratio=1.0, choice="a")
    wrong = messages(text="toolong", count=-1, ratio="x", choice="z", extra=1)

    assert absent["missing"] == "Field required"
    assert short["string_too_short"] == "String should have at least 1 character"
    assert wrong["string_too_long"] == "String should have at most 5 characters"
    assert wrong["greater_than_equal"] == "Input should be greater than or equal to 0"
    assert wrong["float_parsing"] == "Input should be a valid number, unable to parse string as a number"
    assert wrong["literal_error"] == "Input should be 'a' or 'b'"
    assert wrong["extra_forbidden"] == "Extra inputs are not permitted"


def test_the_upper_bound_sentence() -> None:
    over = messages(text="ok", count=99, ratio=1.0, choice="a")

    assert over["less_than_equal"] == "Input should be less than or equal to 10"


def test_an_enumeration_of_three_keeps_the_comma_and_or_shape() -> None:
    """The editor turns ` or ` into ` 또는 ` and leaves the commas; that only works for this shape."""

    class Method(BaseModel):
        method: Literal["GET", "POST", "PATCH"]

    with pytest.raises(ValidationError) as caught:
        Method(method="NOPE")  # type: ignore[arg-type]

    assert caught.value.errors()[0]["msg"] == "Input should be 'GET', 'POST' or 'PATCH'"


def test_a_custom_validator_message_is_prefixed_with_value_error() -> None:
    """The editor strips that prefix, because what follows is the engine's own Korean."""
    from pydantic import field_validator

    class Checked(BaseModel):
        name: str

        @field_validator("name")
        @classmethod
        def _not_empty(cls, value: str) -> str:
            if not value:
                raise ValueError("이름이 필요합니다")
            return value

    with pytest.raises(ValidationError) as caught:
        Checked(name="")

    assert caught.value.errors()[0]["msg"] == "Value error, 이름이 필요합니다"
