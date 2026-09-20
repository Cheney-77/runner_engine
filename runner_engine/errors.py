class RunnerError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class CatalogError(RunnerError):
    pass


class LeaseError(RunnerError):
    pass


class QuotaError(RunnerError):
    pass


class SandboxError(RunnerError):
    pass
