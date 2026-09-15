from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from engine.jsondata import safe_text

Severity = Literal["error", "warning"]

MAX_ISSUES = 100  # issues reported for one workflow; errors are kept before warnings


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: str
    message: str
    nodeId: str | None = None
    edgeId: str | None = None
    field: str | None = None

    def __post_init__(self) -> None:
        for name in ("message", "nodeId", "edgeId", "field"):  # may quote untrusted text; sent as UTF-8
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, safe_text(value))

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def error(code: str, message: str, **where: Any) -> Issue:
    return Issue("error", code, message, **where)


def warning(code: str, message: str, **where: Any) -> Issue:
    return Issue("warning", code, message, **where)


def has_errors(issues: list[Issue]) -> bool:
    return any(issue.severity == "error" for issue in issues)


def bounded(issues: list[Issue]) -> list[Issue]:
    """De-duplicated, errors before warnings (each in the order found), at most MAX_ISSUES. Never drops
    every error, so `has_errors` on the result is the same as on the input."""
    unique = list(dict.fromkeys(issues))
    errors = [issue for issue in unique if issue.severity == "error"]
    return (errors + [issue for issue in unique if issue.severity != "error"])[:MAX_ISSUES]
