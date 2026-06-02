"""Error types and exit codes."""

EXIT_PARSE_ERROR = 2
EXIT_VALIDATION_ERROR = 3
EXIT_INPUT_VALUE_ERROR = 2

VALIDATION_ERRORS = {
    "DeclarationAfterAssignmentError", "DuplicateNameError", "UndefinedNameError",
    "UnassignedSignalError", "InputAssignmentError", "MultipleAssignmentError",
    "ArityError", "CycleError", "WidthMismatchError", "IndexOutOfBoundsError",
    "RedefinitionError", "JsonSchemaError",
}

ERROR_EXIT_CODES = {
    "CircParseError": EXIT_PARSE_ERROR,
    "JsonParseError": EXIT_PARSE_ERROR,
    "BenchParseError": EXIT_PARSE_ERROR,
    "MissingInputError": 1,
    "UnknownInputError": 1,
    "UnknownInputFormatError": 1,
    "InputValueParseError": EXIT_INPUT_VALUE_ERROR,
    "InputWidthMismatchError": EXIT_VALIDATION_ERROR,
    "RadixNotAllowedIn3ValError": 1,
}


class CircError(Exception):
    def __init__(self, error_type: str, message: str, file: str = None, line: int = None, col: int = None):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.file = file
        self.line = line
        self.col = col

    def to_dict(self) -> dict:
        return {"type": self.error_type, "message": self.message, "file": self.file, "line": self.line, "col": self.col}


class EvalError(Exception):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type
        self.message = message

    def to_dict(self) -> dict:
        return {"type": self.error_type, "message": self.message}


def get_exit_code(error_type: str) -> int:
    if error_type in VALIDATION_ERRORS:
        return EXIT_VALIDATION_ERROR
    return ERROR_EXIT_CODES.get(error_type, 1)
