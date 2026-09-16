from enum import StrEnum

from engine.jsondata import safe_text


class ErrorCode(StrEnum):
    NODE_TIMEOUT = "NODE_TIMEOUT"
    NODE_FAILED = "NODE_FAILED"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    TEMPLATE_ERROR = "TEMPLATE_ERROR"
    STRUCTURED_OUTPUT_FAILED = "STRUCTURED_OUTPUT_FAILED"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
    ENGINE_RECURSION_LIMIT = "ENGINE_RECURSION_LIMIT"
    RUN_TIMEOUT = "RUN_TIMEOUT"
    ENGINE_RECOVERY_EXHAUSTED = "ENGINE_RECOVERY_EXHAUSTED"


class NodeError(Exception):
    """Failure of a single node attempt. `retryable` decides whether the policy may retry it."""

    def __init__(self, code: ErrorCode, message: str, *, retryable: bool) -> None:
        message = safe_text(message)  # may quote untrusted text; stored as jsonb and sent as UTF-8
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def to_dict(self) -> dict[str, str]:
        return {"code": str(self.code), "message": self.message}


class NodeFailedError(Exception):
    """Raised by the node wrapper when a node exhausted its attempts with onError=fail."""

    def __init__(self, node_id: str, error: NodeError) -> None:
        super().__init__(f"node {node_id} failed: {error.message}")
        self.node_id = node_id
        self.error = error


class EngineFault(Exception):
    """Infrastructure failure inside a node call (e.g. the recorder's database write). Not the user's node
    error: it escapes the run so the worker stops and crash recovery (lease expiry) continues the run."""


class RunCancelled(Exception):
    """Raised when the run guard observes a cancellation request (or, as LeaseLost, a lost lease)."""


class LeaseLost(RunCancelled):
    """This worker no longer owns the run. Stop without writing a terminal status: the new owner continues."""
