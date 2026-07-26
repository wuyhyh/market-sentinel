class MarketDataProviderError(Exception):
    """Base error for a provider operation that did not yield trustworthy data."""


class MarketDataTimeoutError(MarketDataProviderError):
    """The provider did not respond before the configured deadline."""


class MarketDataRateLimitError(MarketDataProviderError):
    """The provider rejected the request because of a rate limit."""


class MarketDataAuthorizationError(MarketDataProviderError):
    """The provider rejected the configured credentials or permissions."""


class MarketDataProtocolError(MarketDataProviderError):
    """The provider response did not conform to its documented protocol."""


class MarketDataQualityError(MarketDataProviderError):
    """Provider data failed deterministic domain quality validation."""
