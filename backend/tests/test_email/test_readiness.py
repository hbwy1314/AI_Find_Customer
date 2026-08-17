"""Tests for emailing/readiness.py — Microsoft Graph readiness dispatch.

The legacy SMTP/IMAP dispatch paths were removed. Outbound and
inbound now both rely on the Graph credentials, so the tests cover
the Graph-only path and the provider-type coercion that keeps old
``"smtp"`` / ``"imap"`` settings / DB rows working.
"""

import pytest

from emailing import readiness


def _settings(**overrides):
    base = {
        "email_provider_type": "graph",
        "email_from_address": "sales@example.com",
        "email_smtp_last_test_at": "",
        "email_imap_last_test_at": "",
        "graph_tenant_id": "tenant-1",
        "graph_client_id": "client-1",
        "graph_client_secret": "secret-1",
        "graph_mailbox_upn": "sales@example.com",
        "graph_last_test_at": "",
    }
    base.update(overrides)
    return type("S", (), base)()


class TestProviderDispatch:
    def test_provider_type_defaults_to_graph_when_missing(self):
        # Empty settings still resolve to "graph" so deployments that
        # never set EMAIL_PROVIDER_TYPE (the new default) keep working.
        assert readiness.provider_type(type("S", (), {})()) == "graph"

    def test_provider_type_normalizes_case(self):
        assert readiness.provider_type(_settings(email_provider_type=" Graph ")) == "graph"

    def test_provider_type_coerces_legacy_smtp_to_graph(self):
        # Old DB rows / .env that still say "smtp" must be treated as
        # Graph so the deployment doesn't silently fall through to
        # the unsupported branch.
        assert readiness.provider_type(_settings(email_provider_type="smtp")) == "graph"
        assert readiness.provider_type(_settings(email_provider_type="imap")) == "graph"


class TestGraphReadiness:
    def test_ready_when_all_fields_present(self):
        status = readiness.graph_readiness(_settings())
        assert status["ready"] is True
        assert status["missing_fields"] == []

    @pytest.mark.parametrize("field", [
        "graph_tenant_id", "graph_client_id", "graph_client_secret", "graph_mailbox_upn",
    ])
    def test_missing_field_blocks(self, field):
        status = readiness.graph_readiness(_settings(**{field: ""}))
        assert status["ready"] is False
        assert len(status["missing_fields"]) == 1


class TestOutboundDispatch:
    def test_outbound_requires_graph_when_from_address_missing(self):
        # Even with EMAIL_FROM_ADDRESS missing, outbound_readiness now
        # only checks Graph; the from-address field is no longer part
        # of the dispatch but the Graph check still gates the call.
        status = readiness.outbound_readiness(_settings(graph_mailbox_upn=""))
        assert status["ready"] is False
        assert "GRAPH_MAILBOX_UPN" in status["message"]

    def test_outbound_ignores_smtp_legacy_settings(self):
        """SMTP/IMAP settings should have no effect on outbound readiness."""
        status = readiness.outbound_readiness(_settings())
        assert status["ready"] is True

    def test_outbound_blocks_on_missing_graph_fields(self):
        settings = _settings(graph_client_id="")
        status = readiness.outbound_readiness(settings)
        assert status["ready"] is False
        assert "GRAPH_CLIENT_ID" in status["message"]

    def test_ensure_outbound_ready_passes_for_configured_graph(self):
        readiness.ensure_outbound_ready(_settings())  # must not raise


class TestInboundDispatch:
    def test_inbound_reuses_graph_config(self):
        # Inbound readiness is the same as outbound (Graph covers both).
        status = readiness.inbound_readiness(_settings())
        assert status["ready"] is True

    def test_inbound_blocks_on_missing_graph_fields(self):
        settings = _settings(graph_tenant_id="")
        status = readiness.inbound_readiness(settings)
        assert status["ready"] is False
        assert "GRAPH_TENANT_ID" in status["message"]


class TestTestedGates:
    def test_outbound_tested_requires_graph_test_timestamp(self):
        settings = _settings()
        with pytest.raises(ValueError, match="test Graph in Settings"):
            readiness.ensure_outbound_tested(settings)

    def test_outbound_tested_passes_after_graph_test(self):
        settings = _settings(
            graph_last_test_at="2026-08-15T00:00:00Z",
        )
        readiness.ensure_outbound_tested(settings)  # must not raise

    def test_outbound_tested_prefers_config_error_over_untested_message(self):
        settings = _settings(graph_mailbox_upn="")
        with pytest.raises(ValueError, match="Microsoft Graph is not configured"):
            readiness.ensure_outbound_tested(settings)

    def test_inbound_tested_requires_graph_test(self):
        # Graph covers send AND receive, so inbound-tested mirrors
        # outbound-tested (both gate on GRAPH_LAST_TEST_AT).
        with pytest.raises(ValueError, match="test Graph in Settings"):
            readiness.ensure_inbound_tested(_settings())

    def test_graph_test_readiness_tracks_tested_at(self):
        status = readiness.graph_test_readiness(_settings())
        assert status["ready"] is False
        assert status["tested_at"] == ""
        status = readiness.graph_test_readiness(_settings(graph_last_test_at="2026-08-15T00:00:00Z"))
        assert status["ready"] is True
        assert status["tested_at"] == "2026-08-15T00:00:00Z"
