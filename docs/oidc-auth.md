# Native OIDC Authentication

Project Bot unterstützt native OpenID Connect (OIDC) Authentifizierung direkt im Flask-Backend (`oidc_auth.py`, basierend auf Authlib mit PKCE `S256` und signierten Session-Cookies).

Standardmäßig ist OIDC deaktiviert (`OIDC_ENABLED=false`), sodass lokaler Betrieb (`127.0.0.1:8002`) ohne Konfiguration funktioniert.

## Aktivierung über Umgebungsvariablen

| Variable | Pflicht | Default | Beschreibung |
| --- | --- | --- | --- |
| `OIDC_ENABLED` | ja | `false` | `true` aktiviert den Login-Schutz für alle Routen außer `/api/v1/health` und `/auth/*` |
| `OIDC_ISSUER_URL` | ja* | – | Issuer-URL deines IdP (z. B. Authentik, Keycloak, Authelia, Pocket ID). Lädt `<issuer>/.well-known/openid-configuration` |
| `OIDC_METADATA_URL` | optional* | – | Explizite Discovery-URL (Alternative zu `OIDC_ISSUER_URL`) |
| `OIDC_CLIENT_ID` | ja | – | OIDC Client ID |
| `OIDC_CLIENT_SECRET` | optional | – | OIDC Client Secret (bei Public-PKCE-Clients leer lassen) |
| `FLASK_SECRET_KEY` | empfohlen | zufällig | Fester Secret Key zum Signieren der Session-Cookies (z. B. `openssl rand -hex 32`) |
| `OIDC_SCOPES` | nein | `openid email profile` | Angeforderte OIDC-Scopes (z. B. `openid email profile groups` für Gruppenprüfung) |
| `OIDC_ALLOWED_EMAILS` | nein | – | Komma-separierte E-Mail-Allowlist (z. B. `du@example.com`) |
| `OIDC_ALLOWED_GROUPS` | nein | – | Komma-separierte Gruppen-Allowlist (prüft den `groups`-Claim) |
| `OIDC_REDIRECT_URI` | nein | automatisch | Optionaler Override für die Callback-URL (`https://<host>/auth/callback`) |
| `OIDC_POST_LOGOUT_REDIRECT_URI` | nein | – | Ziel-URL nach Provider-Logout |
| `OIDC_TRUST_PROXY` | nein | `true` | Aktiviert Werkzeug `ProxyFix` für `X-Forwarded-Proto/Host` hinter Gateway/Ingress |
| `OIDC_SESSION_COOKIE_SECURE` | nein | `true` | Für lokales HTTP-Testen auf `false` setzen |
| `OIDC_SESSION_LIFETIME_HOURS` | nein | `24` | Lebensdauer der Login-Session in Stunden |

## Redirect-URI im Identity Provider

Als erlaubte Redirect-URI im IdP eintragen:

```text
https://<deine-domain>/auth/callback
```

## Endpunkte

- `GET /auth/login` – startet den OIDC-Login (Authorization Code + PKCE `S256`)
- `GET /auth/callback` – verarbeitet den Callback, prüft optionale Allowlists und setzt das Session-Cookie
- `GET /auth/logout` – löscht die Session (und leitet optional an `end_session_endpoint` weiter)
- `GET /auth/me` – liefert Status und Benutzerinfos der aktuellen Session
- `GET /api/v1/health` – bleibt für Kubernetes-Probes & Docker-Healthchecks immer öffentlich erreichbar

## Beispiel für Kubernetes / Helm `values.yaml`

```yaml
env:
  OIDC_ENABLED: "true"
  OIDC_ISSUER_URL: "https://auth.example.local/application/o/project-bot"
  OIDC_SCOPES: "openid email profile groups"
  OIDC_ALLOWED_EMAILS: "daniel@example.local"

# Secret (empfohlen via existingSecret):
secrets:
  OIDC_CLIENT_ID: "project-bot"
  OIDC_CLIENT_SECRET: "..."
  FLASK_SECRET_KEY: "<64-hex-chars>"
```
