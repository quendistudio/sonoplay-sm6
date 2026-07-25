"""Exponential backoff after DLNA/SM6 SOAP connection failures."""
from __future__ import annotations


def soap_backoff_seconds(
    consecutive_failures: int,
    *,
    base: float = 0.5,
    maximum: float = 30.0,
) -> float:
    """Seconds to wait before the next poll burst after *consecutive_failures*."""
    if consecutive_failures <= 0:
        return 0.0
    exponent = min(consecutive_failures - 1, 6)
    return min(maximum, base * (2**exponent))
