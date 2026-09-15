from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: str
    message: str
    nodeId: str | None = None
    edgeId: str | None = None
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def error(code: str, message: str, **where: Any) -> Issue:
    return Issue("error", code, message, **where)


def warning(code: str, message: str, **where: Any) -> Issue:
    return Issue("warning", code, message, **where)


def has_errors(issues: list[Issue]) -> bool:
    return any(issue.severity == "error" for issue in issues)
