class MigrationError(Exception):
    exit_code = 1


class ConfigurationError(MigrationError):
    exit_code = 2


class OperationLocked(MigrationError):
    exit_code = 75


class ApiError(MigrationError):
    def __init__(
        self, message: str, *, uncertain: bool = False, transient: bool = False
    ) -> None:
        super().__init__(message)
        self.uncertain = uncertain
        self.transient = transient
