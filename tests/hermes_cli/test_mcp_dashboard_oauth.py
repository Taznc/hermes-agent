"""Dashboard HTTP contract for hosted MCP OAuth."""

from unittest.mock import patch

import pytest
import hermes_cli.web_server_mcp as _web_server_mcp
import hermes_cli.web_server_profiles as _web_server_profiles


def _client():
    from starlette.testclient import TestClient

    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return client


@pytest.fixture(autouse=True)
def _clear_flows():
    from hermes_cli import web_server

    _web_server_mcp._mcp_oauth_flows.clear()
    web_server.app.state.auth_required = False
    yield
    _web_server_mcp._mcp_oauth_flows.clear()
    web_server.app.state.auth_required = False


def test_hosted_auth_start_returns_public_authorization_url(monkeypatch):
    from hermes_cli import web_server

    client = _client()
    client.post(
        "/api/mcp/servers",
        json={"name": "reports", "url": "https://mcp.example/mcp", "auth": "oauth"},
    )

    def fake_worker(flow, cfg):
        import asyncio

        asyncio.run(flow.publish_authorization_url("https://idp.example/authorize?state=s1"))

    monkeypatch.setattr(_web_server_mcp, "_run_dashboard_mcp_oauth", fake_worker)
    with patch(
        "hermes_cli.dashboard_auth.prefix.resolve_public_url",
        return_value="https://agent.example",
    ):
        response = client.post("/api/mcp/servers/reports/auth")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "authorization_required"
    assert body["authorization_url"] == "https://idp.example/authorize?state=s1"
    flow = _web_server_mcp._mcp_oauth_flows[body["flow_id"]]
    assert flow.redirect_uri == "https://agent.example/api/mcp/oauth/callback/reports"


def test_hosted_callback_bypasses_gated_cookie_auth(monkeypatch):
    import asyncio

    from starlette.testclient import TestClient

    from hermes_cli import web_server
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow

    flow = DashboardOAuthFlow(
        flow_id="flow-gated",
        server_name="reports",
        profile=None,
        hermes_home="/tmp/hermes-test",
        redirect_uri="https://agent.example/api/mcp/oauth/callback/reports",
    )
    asyncio.run(
        flow.publish_authorization_url(
            "https://idp.example/authorize?state=expected"
        )
    )
    _web_server_mcp._mcp_oauth_flows[flow.flow_id] = flow
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)

    response = TestClient(web_server.app).get(
        "/api/mcp/oauth/callback/reports?code=abc&state=expected"
    )

    assert response.status_code == 200
    assert flow._callback == ("abc", "expected")


def test_hosted_auth_allows_same_server_name_in_different_profiles(tmp_path, monkeypatch):
    from hermes_cli import web_server
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow

    profile_home = tmp_path / "profiles" / "work"
    profile_home.mkdir(parents=True)
    monkeypatch.setattr(_web_server_profiles, "_resolve_profile_dir", lambda _name: profile_home)

    existing = DashboardOAuthFlow(
        flow_id="existing-default",
        server_name="reports",
        profile=None,
        hermes_home=str(tmp_path / "default"),
        redirect_uri="https://agent.example/callback/existing",
    )
    _web_server_mcp._mcp_oauth_flows[existing.flow_id] = existing

    def fake_worker(flow, cfg):
        import asyncio

        asyncio.run(flow.publish_authorization_url("https://idp.example/authorize?state=work"))

    with patch("hermes_cli.mcp_config._get_mcp_servers", return_value={"reports": {"url": "https://mcp.example"}}), \
         patch.object(_web_server_mcp, "_run_dashboard_mcp_oauth", fake_worker):
        response = _client().post("/api/mcp/servers/reports/auth?profile=work")

    assert response.status_code != 409




def test_flow_status_does_not_expose_authorization_code():
    from hermes_cli import web_server
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow

    flow = DashboardOAuthFlow(
        flow_id="flow-status",
        server_name="reports",
        profile=None,
        hermes_home="/tmp/hermes-test",
        redirect_uri="https://agent.example/api/mcp/oauth/callback/flow-status",
    )
    flow.authorization_url = "https://idp.example/authorize"
    flow.status = "approved"
    flow._callback = ("secret-code", "secret-state")
    _web_server_mcp._mcp_oauth_flows[flow.flow_id] = flow

    response = _client().get("/api/mcp/oauth/flows/flow-status")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert "secret-code" not in response.text
    assert "secret-state" not in response.text


# ---------------------------------------------------------------------------
# client_public_origin: browser-declared callback fallback (t_d40923b6).
#
# Behind the web-served Desktop renderer's `/api` proxy (changeOrigin: true),
# request.base_url is the private loopback backend's own address, not
# externally reachable — reconstruction from headers alone silently registers
# a dead callback. The browser-native OAuth caller sends its own
# window.location.origin as client_public_origin so the callback resolves to
# somewhere the OAuth provider can actually redirect back to.
# ---------------------------------------------------------------------------


def test_client_public_origin_used_when_no_dashboard_public_url(monkeypatch):
    """No dashboard.public_url configured (the common case for this spike
    deployment): client_public_origin must win over request reconstruction,
    which would otherwise resolve to the loopback backend."""
    from hermes_cli import web_server

    client = _client()
    client.post(
        "/api/mcp/servers",
        json={"name": "reports", "url": "https://mcp.example/mcp", "auth": "oauth"},
    )

    def fake_worker(flow, cfg):
        import asyncio

        asyncio.run(flow.publish_authorization_url("https://idp.example/authorize?state=s1"))

    monkeypatch.setattr(_web_server_mcp, "_run_dashboard_mcp_oauth", fake_worker)
    with patch("hermes_cli.dashboard_auth.prefix.resolve_public_url", return_value=""):
        response = client.post(
            "/api/mcp/servers/reports/auth",
            params={"client_public_origin": "https://hermes-desktop-dev.jashworth.com"},
        )

    assert response.status_code == 200
    body = response.json()
    flow = _web_server_mcp._mcp_oauth_flows[body["flow_id"]]
    assert flow.redirect_uri == "https://hermes-desktop-dev.jashworth.com/api/mcp/oauth/callback/reports"
    # Never the loopback TestClient default base_url.
    assert "testserver" not in flow.redirect_uri
    assert "127.0.0.1" not in flow.redirect_uri


def test_dashboard_public_url_still_wins_over_client_public_origin(monkeypatch):
    """dashboard.public_url is the operator's authoritative declaration —
    a browser-supplied origin must never override it."""
    from hermes_cli import web_server

    client = _client()
    client.post(
        "/api/mcp/servers",
        json={"name": "reports", "url": "https://mcp.example/mcp", "auth": "oauth"},
    )

    def fake_worker(flow, cfg):
        import asyncio

        asyncio.run(flow.publish_authorization_url("https://idp.example/authorize?state=s1"))

    monkeypatch.setattr(_web_server_mcp, "_run_dashboard_mcp_oauth", fake_worker)
    with patch("hermes_cli.dashboard_auth.prefix.resolve_public_url", return_value="https://operator.example"):
        response = client.post(
            "/api/mcp/servers/reports/auth",
            params={"client_public_origin": "https://attacker.example"},
        )

    assert response.status_code == 200
    body = response.json()
    flow = _web_server_mcp._mcp_oauth_flows[body["flow_id"]]
    assert flow.redirect_uri == "https://operator.example/api/mcp/oauth/callback/reports"


def test_malformed_client_public_origin_falls_through_to_reconstruction(monkeypatch):
    """An injection-suspect or path-carrying client_public_origin is dropped,
    not trusted — same posture as a malformed dashboard.public_url."""
    from hermes_cli import web_server

    client = _client()
    client.post(
        "/api/mcp/servers",
        json={"name": "reports", "url": "https://mcp.example/mcp", "auth": "oauth"},
    )

    def fake_worker(flow, cfg):
        import asyncio

        asyncio.run(flow.publish_authorization_url("https://idp.example/authorize?state=s1"))

    monkeypatch.setattr(_web_server_mcp, "_run_dashboard_mcp_oauth", fake_worker)
    for bad_origin in [
        "javascript:alert(1)",
        "https://evil.example/some/path",
        'https://evil.example/"injected',
        "not-a-url",
    ]:
        client.post("/api/mcp/servers", json={"name": "r2", "url": "https://mcp.example/mcp", "auth": "oauth"})
        with patch("hermes_cli.dashboard_auth.prefix.resolve_public_url", return_value=""):
            response = client.post(
                "/api/mcp/servers/r2/auth",
                params={"client_public_origin": bad_origin},
            )
        assert response.status_code == 200
        body = response.json()
        flow = _web_server_mcp._mcp_oauth_flows[body["flow_id"]]
        assert "evil.example" not in flow.redirect_uri, f"malformed origin {bad_origin!r} leaked into redirect_uri"
        assert "javascript" not in flow.redirect_uri
        client.delete("/api/mcp/servers/r2")
        _web_server_mcp._mcp_oauth_flows.pop(body["flow_id"], None)

