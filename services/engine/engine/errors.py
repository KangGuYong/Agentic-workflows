from enum import StrEnum


class ErrorCode(StrEnum):
    NODE_TIMEOUT = "NODE_TIMEOUT"
    NODE_FAILED = "NODE_FAILED"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    TEMPLATE_ERROR = "TEMPLATE_ERROR"
    STRUCTURED_OUTPUT_FAILED = "STRUCTURED_OUTPUT_FAILED"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
    ENGINE_RECURSION_LIMIT = "ENGINE_RECURSION_LIMIT"


class NodeError(Exception):
    """Failure of a single node attempt. `retryable` decides whether the policy may retry it."""

    def __init__(self, code: ErrorCode, message: str, *, retryable: bool) -> None:
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


class RunCancelled(Exception):
    """Raised when the run guard observes a cancellation request or a lost lease."""
