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


# ERROR CODES - CTE_ERROR GROUP (code 4)
INVALID_CTE_SYNTAX = 400
RECURSIVE_DEPTH_EXCEEDED = 401
CTE_NOT_FOUND = 402
CYCLIC_REFERENCE = 403


class CTEError(SQLError):
    """Common Table Expression error."""

    def __init__(self, code: int, message: str):
        super().__init__(code, "CTE_ERROR", message)


class InvalidCTESyntaxError(CTEError):
    """Invalid CTE syntax."""

    def __init__(self, message: str):
        super().__init__(INVALID_CTE_SYNTAX, message)


class RecursiveDepthExceededError(CTEError):
    """Recursive CTE exceeded maximum depth."""

    def __init__(self, message: str = "Recursive CTE exceeded maximum depth (1000)"):
        super().__init__(RECURSIVE_DEPTH_EXCEEDED, message)


class CTENotFoundError(CTEError):
    """CTE not found."""

    def __init__(self, message: str):
        super().__init__(CTE_NOT_FOUND, message)


class CyclicReferenceError(CTEError):
    """Cyclic reference in CTE."""

    def __init__(self, message: str):
        super().__init__(CYCLIC_REFERENCE, message)


# ERROR CODES - SUBQUERY_ERROR GROUP (code 5)
SCALAR_SUBQUERY_MULTIPLE_ROWS = 500
INVALID_SUBQUERY_REFERENCE = 501
CORRELATED_SUBQUERY_ERROR = 502


class SubqueryError(SQLError):
    """Subquery error."""

    def __init__(self, code: int, message: str):
        super().__init__(code, "SUBQUERY_ERROR", message)


class ScalarSubqueryMultipleRowsError(SubqueryError):
    """Scalar subquery returned multiple rows."""

    def __init__(self, message: str = "Scalar subquery returned more than one row"):
        super().__init__(SCALAR_SUBQUERY_MULTIPLE_ROWS, message)


class InvalidSubqueryReferenceError(SubqueryError):
    """Invalid subquery reference."""

    def __init__(self, message: str):
        super().__init__(INVALID_SUBQUERY_REFERENCE, message)


class CorrelatedSubqueryError(SubqueryError):
    """Correlated subquery error."""

    def __init__(self, message: str):
        super().__init__(CORRELATED_SUBQUERY_ERROR, message)
