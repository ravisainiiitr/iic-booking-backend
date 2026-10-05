class ProcurementError(Exception):
    """Business-rule failure surfaced to API clients as ``{"detail", "code"}``."""

    def __init__(self, message: str, *, status: int = 400, code: str = "invalid", **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.extra = extra


def forbidden(message: str = "You do not have permission to do this.") -> ProcurementError:
    return ProcurementError(message, status=403, code="forbidden")


def not_found(message: str = "Not found.") -> ProcurementError:
    return ProcurementError(message, status=404, code="not_found")
