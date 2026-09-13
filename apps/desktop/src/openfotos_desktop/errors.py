"""Desktop boundary errors shared by control-plane and sync services."""


class DesktopApiError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class SourceChangedError(DesktopApiError):
    def __init__(self) -> None:
        super().__init__(
            "source_changed",
            "The source bytes changed after approval; add the changed photograph to a new batch.",
        )
