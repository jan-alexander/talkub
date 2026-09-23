"""Errors that are reported to the user without a traceback."""


class TalkubError(Exception):
    """Base class. `str(err)` is the user-facing message, `hint` an optional next step."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class RepoNotFound(TalkubError):
    """No cluster repo (.talkub.yaml) could be located."""


class ValidationError(TalkubError):
    """Invalid input, settings or repo state."""


class NoIdentity(TalkubError):
    """No age identity or master key is available."""


class DecryptFailed(TalkubError):
    """age or sops could not decrypt a file."""


class ToolMissing(TalkubError):
    """A required external binary is not installed."""


class CommandFailed(TalkubError):
    """An external command exited with a non-zero status."""
