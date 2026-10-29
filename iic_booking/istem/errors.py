"""Structured I-STEM integration errors. Messages are safe to show administrators: no secrets, no payloads."""

from __future__ import annotations

from .choices import ErrorCategory

# The request cannot have reached I-STEM's application: safe to send again.
NOT_SENT = frozenset({ErrorCategory.CONFIG, ErrorCategory.CONNECT_FAILED, ErrorCategory.TLS, ErrorCategory.AUTH_UNAVAILABLE})
# I-STEM answered and refused (business error, validation, not found): the operation did not happen.
DEFINITE_FAILURE = frozenset({ErrorCategory.VALIDATION, ErrorCategory.BUSINESS_ERROR, ErrorCategory.NOT_FOUND, ErrorCategory.CONFLICT})
# Authentication was refused; the request was not accepted, but credentials or the token need attention.
AUTH_FAILURE = frozenset({ErrorCategory.AUTH, ErrorCategory.FORBIDDEN})
# The request may have been processed remotely without us learning the result.
POSSIBLY_PROCESSED = frozenset({
    ErrorCategory.READ_TIMEOUT,
    ErrorCategory.CONNECTION_LOST,
    ErrorCategory.SERVER_ERROR,
    ErrorCategory.INVALID_RESPONSE,
    ErrorCategory.RATE_LIMITED,
    ErrorCategory.WORKER_LOST,
})


class IstemError(Exception):
    """A failed I-STEM call or a refused local precondition."""

    def __init__(self, category: str, message: str, *, http_status: int | None = None, fields: tuple[str, ...] = ()):
        super().__init__(message)
        self.category = category
        self.message = message
        self.http_status = http_status
        self.fields = tuple(fields)

    @property
    def not_sent(self) -> bool:
        return self.category in NOT_SENT

    @property
    def possibly_processed(self) -> bool:
        return self.category in POSSIBLY_PROCESSED

    @property
    def definite_failure(self) -> bool:
        return self.category in DEFINITE_FAILURE

    @property
    def auth_failure(self) -> bool:
        return self.category in AUTH_FAILURE

    def __str__(self) -> str:
        return f"{self.category}: {self.message}"


class IstemConfigError(IstemError):
    """Integration disabled, not in live mode, or a required setting / mapping / local field is missing."""

    def __init__(self, message: str, *, fields: tuple[str, ...] = ()):
        super().__init__(ErrorCategory.CONFIG, message, fields=fields)
