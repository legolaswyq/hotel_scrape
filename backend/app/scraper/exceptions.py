class ScraperBlockedError(Exception):
    """Raised when the target site blocks or rejects the automated request."""


class ScraperTimeoutError(Exception):
    """Raised when expected page content never appears within the timeout."""


class ScraperInterruptedError(Exception):
    """Raised when the browser session ends unexpectedly mid-scrape (the
    window was closed manually, the browser process crashed, etc.) -- not a
    site block, just the automation losing its browser."""
