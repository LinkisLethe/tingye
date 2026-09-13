class Cancelled(Exception):
    """Raised by a cooperative task cancellation checkpoint."""


class UserError(Exception):
    """An actionable, safe-to-display error message."""
