class TrainingError(Exception):
    """Business-rule violation; views answer with ``status`` and ``message``."""

    def __init__(self, message: str, *, status: int = 400, code: str = "invalid", extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.extra = extra or {}
