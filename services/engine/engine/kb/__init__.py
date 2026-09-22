"""Knowledge bases: ingestion helpers, the vector store and the clients the nodes use."""
from __future__ import annotations


class IngestError(Exception):
    """One file could not be ingested. `retryable` follows the same rule as NodeError: transport
    failures and 5xx are worth another attempt, a document the parser rejects is not."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
