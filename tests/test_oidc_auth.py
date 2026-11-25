"""Tests for native OIDC authentication (oidc_auth.py)."""

from unittest.mock import MagicMock

import pytest
from flask import Flask, jsonify

from oidc_auth import _is_user_authorized, _safe_next_url, init_oidc


def _make_app() -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index():
        return "<html>dashboard</html>"

    @app.route("/api/v1/health")
    def health():
        return jsonify({"status": "healthy"})

    @app.route("/api/v1/projects")
    def projects():
        return jsonify({"projects": []})

    init_oidc(app)
    return app


def _set_oidc_env(monkeypatch, **extra):
    monkeypatch.setenv("OIDC_ENABLED", "true")
    monkeypatch.setenv("OIDC_ISSUER_URL", "https://auth.example.local/application/o/project-bot")
    monkeypatch.setenv("OIDC_CLIENT_ID", "project-bot-client")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "super-secret")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-key-for-sessions")
    monkeypatch.setenv("OIDC_SESSION_COOKIE_SECURE", "false")
    for key, val in extra.items():
        monkeypatch.setenv(key, val)


class TestOidcDisabled:
    def test_default_disabled_allows_all_routes(self, monkeypatch):
        monkeypatch.delenv("OIDC_ENABLED", raising=False)
        app = _make_app()
        client = app.test_client()

        assert client.get("/").status_code == 200
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/projects").status_code == 200

        me = client.get("/auth/me")
        assert me.status_code == 200
        assert me.get_json() == {
            "authenticated": False,
            "oidc_enabled": False,
            "user": None,
        }


class TestOidcValidation:
    def test_missing_issuer_raises(self, monkeypatch):
        monkeypatch.setenv("OIDC_ENABLED", "true")
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.delenv("OIDC_METADATA_URL", raising=False)
        monkeypatch.setenv("OIDC_CLIENT_ID", "cid")
        with pytest.raises(RuntimeError, match="OIDC_ISSUER_URL"):
            _make_app()

    def test_missing_client_id_raises(self, monkeypatch):
        monkeypatch.setenv("OIDC_ENABLED", "true")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://auth.example.local")
        monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
        with pytest.raises(RuntimeError, match="OIDC_CLIENT_ID"):
            _make_app()


class TestOidcEnabledGuard:
    def test_unauthenticated_requests_are_blocked_except_health(self, monkeypatch):
        _set_oidc_env(monkeypatch)
        app = _make_app()
        client = app.test_client()

        # Health endpoint stays public for K8s/Docker probes
        assert client.get("/api/v1/health").status_code == 200

        # API routes return 401 JSON
        api_resp = client.get("/api/v1/projects")
        assert api_resp.status_code == 401
        body = api_resp.get_json()
        assert body["error"] == "Unauthorized"
        assert body["login_url"] == "/auth/login"

        # /auth/me returns 401 when unauthenticated
        assert client.get("/auth/me").status_code == 401

        # Browser routes redirect to /auth/login
        html_resp = client.get("/projects?status=open")
        assert html_resp.status_code == 302
        assert html_resp.headers["Location"].startswith("/auth/login")

    def test_authenticated_session_grants_access_and_logout_clears(self, monkeypatch):
        _set_oidc_env(monkeypatch)
        app = _make_app()
        client = app.test_client()

        with client.session_transaction() as sess:
            sess["oidc_user"] = {
                "sub": "user-123",
                "email": "daniel@example.local",
                "name": "Daniel",
                "groups": ["admins"],
            }

        assert client.get("/").status_code == 200
        assert client.get("/api/v1/projects").status_code == 200

        me = client.get("/auth/me")
        assert me.status_code == 200
        data = me.get_json()
        assert data["authenticated"] is True
        assert data["oidc_enabled"] is True
        assert data["user"]["email"] == "daniel@example.local"

        # Logout clears session
        app.extensions["oidc_client"].load_server_metadata = MagicMock(return_value={})
        logout_resp = client.get("/auth/logout")
        assert logout_resp.status_code == 302
        assert client.get("/api/v1/projects").status_code == 401


class TestOidcCallbackAndAllowlists:
    def test_callback_sets_session_and_redirects_safely(self, monkeypatch):
        _set_oidc_env(monkeypatch, OIDC_ALLOWED_EMAILS="daniel@example.local")
        app = _make_app()
        app.extensions["oidc_client"].authorize_access_token = MagicMock(
            return_value={
                "id_token": "fake-id-token",
                "userinfo": {
                    "sub": "sub-1",
                    "email": "Daniel@Example.Local",
                    "name": "Daniel",
                    "groups": ["devops"],
                },
            }
        )
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["oidc_next"] = "/dashboard"

        resp = client.get("/auth/callback")
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/dashboard"
        assert client.get("/api/v1/projects").status_code == 200

    def test_callback_rejects_disallowed_email_with_403(self, monkeypatch):
        _set_oidc_env(monkeypatch, OIDC_ALLOWED_EMAILS="onlyme@example.local")
        app = _make_app()
        app.extensions["oidc_client"].authorize_access_token = MagicMock(
            return_value={
                "userinfo": {
                    "sub": "sub-2",
                    "email": "intruder@example.local",
                    "name": "Intruder",
                }
            }
        )
        client = app.test_client()
        resp = client.get("/auth/callback")
        assert resp.status_code == 403
        assert client.get("/api/v1/projects").status_code == 401

    def test_callback_enforces_group_allowlist(self, monkeypatch):
        _set_oidc_env(monkeypatch, OIDC_ALLOWED_GROUPS="project-bot-users,admins")
        app = _make_app()
        app.extensions["oidc_client"].authorize_access_token = MagicMock(
            return_value={
                "userinfo": {
                    "sub": "sub-3",
                    "email": "user@example.local",
                    "groups": ["other-group"],
                }
            }
        )
        client = app.test_client()
        assert client.get("/auth/callback").status_code == 403


class TestOidcHelpers:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, "/"),
            ("", "/"),
            ("/dashboard", "/dashboard"),
            ("/projects?page=2", "/projects?page=2"),
            ("https://evil.com", "/"),
            ("//evil.com", "/"),
            ("/\\evil.com", "/"),
        ],
    )
    def test_safe_next_url(self, raw, expected):
        assert _safe_next_url(raw) == expected

    def test_is_user_authorized(self):
        claims = {"email": "Alice@Example.com", "groups": ["devops", "team"]}
        assert _is_user_authorized(claims, set(), set()) is True
        assert _is_user_authorized(claims, {"alice@example.com"}, set()) is True
        assert _is_user_authorized(claims, {"bob@example.com"}, set()) is False
        assert _is_user_authorized(claims, set(), {"devops"}) is True
        assert _is_user_authorized(claims, set(), {"admins"}) is False
