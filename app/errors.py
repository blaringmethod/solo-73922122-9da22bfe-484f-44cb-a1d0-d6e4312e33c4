"""Small domain error type shared by the service layer and HTTP layer."""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """An expected, client-facing error.

    The HTTP layer converts this into a JSON error response.  Keeping the
    service layer independent from FastAPI makes the accounting rules easy to
    test directly.
    """

    def __init__(self, status_code: int, error_code: str, message: str, **extra: Any) -> None:
        # ``error_code`` is deliberately not called ``code`` so callers can use
        # ``code=...`` in ``extra`` for the offending resource's code.
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.extra = extra

    def detail(self) -> dict[str, Any]:
        return {"code": self.error_code, "message": self.message, **self.extra}
