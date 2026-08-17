"""Tests for the consumer's permanent-vs-transient failure classifier."""

import pytest

from api.app import _is_non_retryable_automation_error


class TestIsNonRetryableAutomationError:
    def test_http_4xx_prefix_is_non_retryable(self):
        # The exact error we hit on the romioecig.com queue
        exc = Exception(
            "409: SMTP is not configured. Missing: "
            "EMAIL_FROM_ADDRESS, EMAIL_SMTP_HOST, EMAIL_SMTP_USERNAME, EMAIL_SMTP_PASSWORD"
        )
        assert _is_non_retryable_automation_error(exc) is True

    @pytest.mark.parametrize(
        "message",
        [
            "400: bad request",
            "401: unauthorized",
            "403: forbidden",
            "404: not found",
            "409: conflict",
            "422: validation failed",
            "Graph token expired: 401: unauthorized",
        ],
    )
    def test_4xx_variants(self, message):
        assert _is_non_retryable_automation_error(Exception(message)) is True

    def test_missing_marker_is_non_retryable(self):
        exc = Exception("EMAIL_SMTP_HOST is required for outbound mail")
        assert _is_non_retryable_automation_error(exc) is True

    def test_not_configured_marker(self):
        exc = Exception("SMTP is not configured. Set EMAIL_SMTP_HOST in .env")
        assert _is_non_retryable_automation_error(exc) is True

    def test_validationerror(self):
        exc = Exception("ValidationError: payload missing required field")
        assert _is_non_retryable_automation_error(exc) is True

    def test_type_errors_are_non_retryable(self):
        assert _is_non_retryable_automation_error(ValueError("bad input")) is True
        assert _is_non_retryable_automation_error(KeyError("missing_key")) is True
        assert _is_non_retryable_automation_error(TypeError("wrong type")) is True

    def test_transient_errors_are_retryable(self):
        # Network timeouts, connection errors, etc. should retry
        for msg in [
            "ConnectionError: Connection refused",
            "TimeoutError: request timed out after 30s",
            "ConnectTimeout: Could not connect to api.example.com",
            "ReadTimeout: server did not respond",
            "503: service temporarily unavailable",
            "502: bad gateway",
        ]:
            assert _is_non_retryable_automation_error(Exception(msg)) is False, msg

    def test_empty_message_is_retryable(self):
        # Defensive default — better to retry a no-op than to mark failed
        assert _is_non_retryable_automation_error(Exception("")) is False
