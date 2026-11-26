"""Tests for DLNA SOAP exponential backoff."""

from dlna.soap_backoff import soap_backoff_seconds


def test_soap_backoff_zero_when_no_failures() -> None:
    assert soap_backoff_seconds(0) == 0.0


def test_soap_backoff_exponential_with_cap() -> None:
    assert soap_backoff_seconds(1, base=0.5, maximum=30.0) == 0.5
    assert soap_backoff_seconds(2, base=0.5, maximum=30.0) == 1.0
    assert soap_backoff_seconds(3, base=0.5, maximum=30.0) == 2.0
    assert soap_backoff_seconds(8, base=0.5, maximum=30.0) == 30.0
