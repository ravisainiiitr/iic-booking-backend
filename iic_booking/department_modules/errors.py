class DepartmentModuleError(Exception):
    """Business-rule failure surfaced to API clients as ``{"detail", "code"}``."""

    def __init__(self, message: str, *, status: int = 400, code: str = "invalid", field: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.field = field
