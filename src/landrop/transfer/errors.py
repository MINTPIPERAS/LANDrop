"""Transfer errors shared by the single-file and directory uploaders."""

from __future__ import annotations


class TransferError(RuntimeError):
    """An upload failed without publishing a partial file as complete.

    ``code`` is stable and safe to branch on in the UI; the message is for
    humans and may change.
    """

    def __init__(self, message: str, code: str = "transfer_failed") -> None:
        super().__init__(message)
        self.code = code


class TransferCancelled(TransferError):
    """The caller requested cancellation; the remote partial is preserved."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="cancelled")
