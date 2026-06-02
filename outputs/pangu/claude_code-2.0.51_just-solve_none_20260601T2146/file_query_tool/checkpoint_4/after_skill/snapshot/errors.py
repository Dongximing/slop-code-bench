"""Error handling for SQL query engine."""


class SQLError(Exception):
    """Base exception for SQL errors."""

    def __init__(self, code: int, group: str, message: str):
        self.code = code
        self.group = group
        self.message = message
        super().__init__(f"[{group}] (code {code}): {message}")


# ERROR CODES - WINDOW_ERROR GROUP (code 3)
INVALID_WINDOW_SPEC = 300
FRAME_ERROR = 301
NESTED_WINDOW_ERROR = 302


class WindowSpecError(SQLError):
    """Invalid window specification."""

    def __init__(self, message: str):
        super().__init__(INVALID_WINDOW_SPEC, "WINDOW_ERROR", message)


class FrameClauseError(SQLError):
    """Invalid frame clause."""

    def __init__(self, message: str):
        super().__init__(FRAME_ERROR, "WINDOW_ERROR", message)


class NestedWindowFunctionError(SQLError):
    """Nested window functions are not supported."""

    def __init__(self, message: str = "Nested window functions are not supported"):
        super().__init__(NESTED_WINDOW_ERROR, "WINDOW_ERROR", message)
