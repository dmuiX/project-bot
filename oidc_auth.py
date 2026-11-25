"""
Native OpenID Connect (OIDC) authentication for Project Bot (Flask).

Disabled by default (OIDC_ENABLED=false) so local loopback development and
existing tests continue to work without configuration.

When OIDC_ENABLED=true:
- Protects all dashboard and API routes via signed Flask session cookies.
- Keeps /api/v1/health and /auth/* endpoints accessible for K8s probes and login.
- Supports PKCE (S256), ProxyFix (HTTPS behind Gateway/Ingress), optional
  email/group allowlists, and clean logout.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import timedelta
from typing import Any, Dict, Iterable, Optional, Set
from urllib.parse import urlencode

from flask import Blueprint, Flask, jsonify, redirect, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

logger = logging.getLogger(__name__)

_PUBLIC_PATHS = frozenset({
    "/api/v1/health",
    "/auth/login",
    "/auth/callback",
    "/auth/logout",
})


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_csv_set(raw: Optional[str], *, lower: bool = False) -> Set[str]:
    if not raw:
        return set()
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if lower:
        return {item.lower() for item in items}
    return set(items)


def _safe_next_url(target: Optional[str]) -> str:
    """Allow only local relative paths to prevent open redirects."""
    if not target:
        return "/"
    target = target.strip()
    if not target.startswith("/") or target.startswith("//") or target.startswith("/\\"):
        return "/"
    return target


def _extract_groups(claims: Dict[str, Any]) -> Set[str]:
    raw = claims.get("groups")
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, Iterable):
        return {str(g) for g in raw if g}
    return set()


def _is_user_authorized(
    claims: Dict[str, Any],
    allowed_emails: Set[str],
    allowed_groups: Set[str],
) -> bool:
    if allowed_emails:
        email = str(claims.get("email") or "").strip().lower()
        if not email or email not in allowed_emails:
            return False
    if allowed_groups:
        user_groups = _extract_groups(claims)
        if not (user_groups & allowed_groups):
            return False
    return True


def init_oidc(app: Flask) -> None:
    """Attach OIDC authentication routes and before_request guard to a Flask app."""
    enabled = _env_bool("OIDC_ENABLED", False)
    app.config["OIDC_ENABLED"] = enabled

    bp = Blueprint("oidc", __name__, url_prefix="/auth")

    if not enabled:
        @bp.route("/me", methods=["GET"])
        def auth_me_disabled():
            return jsonify({
                "authenticated": False,
                "oidc_enabled": False,
                "user": None,
            })

        app.register_blueprint(bp)
        return

    # Lazy import so Authlib is only required when OIDC is enabled (still installed in requirements.txt)
    from authlib.integrations.flask_client import OAuth

    issuer = (os.environ.get("OIDC_ISSUER_URL") or "").strip().rstrip("/")
    metadata_url = (os.environ.get("OIDC_METADATA_URL") or "").strip()
    if not metadata_url:
        if not issuer:
            raise RuntimeError(
                "OIDC_ENABLED=true requires OIDC_ISSUER_URL or OIDC_METADATA_URL"
            )
        metadata_url = f"{issuer}/.well-known/openid-configuration"

    client_id = (os.environ.get("OIDC_CLIENT_ID") or "").strip()
    if not client_id:
        raise RuntimeError("OIDC_ENABLED=true requires OIDC_CLIENT_ID")

    client_secret = (os.environ.get("OIDC_CLIENT_SECRET") or "").strip() or None
    scopes = (os.environ.get("OIDC_SCOPES") or "openid email profile").strip()
    redirect_uri_override = (os.environ.get("OIDC_REDIRECT_URI") or "").strip() or None
    post_logout_uri = (os.environ.get("OIDC_POST_LOGOUT_REDIRECT_URI") or "").strip() or None
    allowed_emails = _parse_csv_set(os.environ.get("OIDC_ALLOWED_EMAILS"), lower=True)
    allowed_groups = _parse_csv_set(os.environ.get("OIDC_ALLOWED_GROUPS"), lower=False)

    # Session configuration
    secret_key = (os.environ.get("FLASK_SECRET_KEY") or "").strip()
    if not secret_key:
        secret_key = secrets.token_hex(32)
        logger.warning(
            "FLASK_SECRET_KEY is not set; generated ephemeral key (sessions will reset on restart)"
        )
    app.secret_key = secret_key
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = _env_bool("OIDC_SESSION_COOKIE_SECURE", True)
    lifetime_hours = int(os.environ.get("OIDC_SESSION_LIFETIME_HOURS", "24"))
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=max(1, lifetime_hours))

    if _env_bool("OIDC_TRUST_PROXY", True):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)  # type: ignore[assignment]

    oauth = OAuth(app)
    client_kwargs: Dict[str, Any] = {
        "scope": scopes,
        "code_challenge_method": "S256",
    }
    oidc_client = oauth.register(
        name="oidc",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url=metadata_url,
        client_kwargs=client_kwargs,
    )
    app.extensions["oidc_client"] = oidc_client

    @bp.route("/login", methods=["GET"])
    def login():
        session["oidc_next"] = _safe_next_url(request.args.get("next"))
        redirect_uri = redirect_uri_override or url_for("oidc.callback", _external=True)
        return oidc_client.authorize_redirect(redirect_uri)

    @bp.route("/callback", methods=["GET"])
    def callback():
        token = oidc_client.authorize_access_token()
        userinfo = token.get("userinfo")
        if not userinfo:
            userinfo = oidc_client.userinfo(token=token)
        claims = dict(userinfo or {})

        if not _is_user_authorized(claims, allowed_emails, allowed_groups):
            session.clear()
            logger.warning(
                "OIDC login denied by allowlist for sub=%s email=%s",
                claims.get("sub"),
                claims.get("email"),
            )
            return jsonify({
                "error": "Forbidden",
                "message": "Your account is not authorized to access this dashboard.",
            }), 403

        next_url = _safe_next_url(session.pop("oidc_next", "/"))
        session.permanent = True
        session["oidc_user"] = {
            "sub": claims.get("sub"),
            "email": claims.get("email"),
            "name": claims.get("name") or claims.get("preferred_username") or claims.get("email"),
            "groups": sorted(_extract_groups(claims)),
        }
        if token.get("id_token"):
            session["oidc_id_token"] = token["id_token"]
        return redirect(next_url)

    @bp.route("/logout", methods=["GET", "POST"])
    def logout():
        id_token = session.get("oidc_id_token")
        session.clear()
        end_session_endpoint = None
        try:
            metadata = oidc_client.load_server_metadata()
            end_session_endpoint = metadata.get("end_session_endpoint")
        except Exception:
            end_session_endpoint = None

        if end_session_endpoint and post_logout_uri:
            params: Dict[str, str] = {"post_logout_redirect_uri": post_logout_uri}
            if id_token:
                params["id_token_hint"] = id_token
            return redirect(f"{end_session_endpoint}?{urlencode(params)}")
        return redirect(url_for("oidc.login"))

    @bp.route("/me", methods=["GET"])
    def auth_me():
        user = session.get("oidc_user")
        if not user:
            return jsonify({
                "authenticated": False,
                "oidc_enabled": True,
                "login_url": url_for("oidc.login"),
            }), 401
        return jsonify({
            "authenticated": True,
            "oidc_enabled": True,
            "user": user,
        })

    app.register_blueprint(bp)

    @app.before_request
    def require_oidc_auth():
        if request.method == "OPTIONS":
            return None
        if request.path in _PUBLIC_PATHS:
            return None
        if session.get("oidc_user"):
            return None

        if request.path.startswith("/api/") or request.path == "/auth/me":
            return jsonify({
                "error": "Unauthorized",
                "message": "Authentication required",
                "login_url": url_for("oidc.login"),
            }), 401

        next_target = _safe_next_url(request.full_path.rstrip("?"))
        return redirect(url_for("oidc.login", next=next_target))

    logger.info("OIDC authentication enabled (issuer=%s)", issuer or metadata_url)
