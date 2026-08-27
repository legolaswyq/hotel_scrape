class ScraperBlockedError(Exception):
    """Raised when the target site blocks or rejects the automated request."""


class ScraperTimeoutError(Exception):
    """Raised when expected page content never appears within the timeout."""
