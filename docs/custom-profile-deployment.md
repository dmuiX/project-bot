# Eigene Profile & Regeln außerhalb von Git (Docker Compose & Kubernetes)

Die im Repository enthaltenen Kompetenzprofile (`competency_profiles/`), Bewertungsregeln (`suitability_rules.yaml`) und Suchgruppen-Filter (`search_group_filters.yaml`) sind beispielhaft bzw. auf ein konkretes Profil zugeschnitten.

Für den eigenen Betrieb müssen **weder persönliche Profile noch CV, API-Keys oder RSS-Tokens ins Git-Repository** eingecheckt werden. Alle relevanten Dateien liegen im Container unter festen Pfaden unter `/app/` und lassen sich direkt über Volumes überlagern.

---

## Übersicht der Mount-Pfade im Container

| Pfad im Container | Inhalt / Zweck | Modus |
| --- | --- | --- |
| `/app/config.yaml` | LLM-Provider/Modelle, Suchgruppen, RSS-URLs, IMAP, Thresholds (API-Keys per `${ENV_VAR}` referenzieren) | `ro` |
| `/app/competency_profiles/` | Eigene Markdown-Kompetenzprofile (`*.md`) für Pre-Scoring & LLM-Bewertung | `ro` |
| `/app/suitability_rules.yaml` | Lokale Gewichtungen, Prioritäten und Muss-Anforderungen | `ro` |
| `/app/search_group_filters.yaml` | Fachliche Include-/Exclude-Begriffe je Suchgruppe | `ro` |
| `/app/data/` | Persistente Laufzeitdaten (`cv.md`, `schedules.json`, `quick_filters.json`, Seen-Ledger) | `rw` |
| `/app/projects/` | Gespeicherte Projekte | `rw` |
| `/app/logs/` | Anwendungslogs | `rw` |

> **Wichtige Hinweise vor dem ersten Start:**
> - **`data/schedules.json`:** Im Repo liegt eine Beispiel-/Alt-Datei mit aktiven Schedules. Vor dem Start im eigenen `/app/data`-Volume eine leere bzw. eigene `schedules.json` (`{"schedules": []}`) anlegen, sonst laufen die Default-Jobs beim Serverstart direkt an.
> - **API-Keys:** Niemals im Klartext in `config.yaml` schreiben, sondern über Umgebungsvariablen (`${OPENAI_API_KEY}`, `${ANTHROPIC_API_KEY}`, `${GOOGLE_API_KEY}`, `${EMAIL_PASSWORD}`) einbinden.
> - **Same-Origin API-URL:** Das `Dockerfile` baut das Vue-Frontend standardmäßig mit `VITE_API_BASE_URL=""` (relative `/api/v1/...`-Pfade) und setzt `FLASK_HOST=0.0.0.0`. Dadurch funktioniert das Dashboard ohne CORS- oder URL-Anpassung hinter beliebigen Ports, Reverse-Proxies oder Kubernetes-`HTTPRoute`s.

---

## Variante 1: Docker Compose (lokale Bind-Mounts)

1. Nicht-versionierten Ordner `local-profile/` im Repo-Root anlegen (`local-profile/` steht in `.gitignore`):

```bash
mkdir -p local-profile/competency_profiles data projects logs
cp config_template.yaml config.yaml
cp suitability_rules.yaml local-profile/suitability_rules.yaml
cp search_group_filters.yaml local-profile/search_group_filters.yaml
cp competency_profiles/*.md local-profile/competency_profiles/
echo '{"schedules": []}' > data/schedules.json
```

2. Eigene Profile/Regeln in `local-profile/` und `config.yaml` anpassen sowie `docker/.env` aus `docker/.env.template` erstellen.

3. In `docker/docker-compose.yml` die optionalen Bind-Mounts einkommentieren:

```yaml
volumes:
  - ../data:/app/data
  - ../projects:/app/projects
  - ../logs:/app/logs
  - ../config.yaml:/app/config.yaml:ro
  - ../local-profile/competency_profiles:/app/competency_profiles:ro
  - ../local-profile/suitability_rules.yaml:/app/suitability_rules.yaml:ro
  - ../local-profile/search_group_filters.yaml:/app/search_group_filters.yaml:ro
```

4. Bauen und starten:

```bash
cd docker
docker compose up -d --build
```

Das Dashboard ist anschließend unter `http://localhost:8002` erreichbar.

---

## Variante 2: Kubernetes (ConfigMaps, Secret & PVC)

In Kubernetes werden die persönlichen Regeln und Profile als `ConfigMap`s (z. B. aus einem privaten GitOps-Repo oder per `kubectl create configmap`), Secrets als `Secret` (oder SOPS/SealedSecret) und die Laufzeitdaten über ein `PersistentVolumeClaim` gemountet.

### 1. ConfigMaps & Secret erstellen (Beispiel per `kubectl`)

```bash
kubectl create namespace project-bot

# Sensible API-Keys & Zugangsdaten
kubectl -n project-bot create secret generic project-bot-secrets \
  --from-literal=OPENAI_API_KEY='...' \
  --from-literal=ANTHROPIC_API_KEY='...' \
  --from-literal=GOOGLE_API_KEY='...' \
  --from-literal=EMAIL_PASSWORD='...'

# YAML-Konfigurationen (config.yaml nutzt ${OPENAI_API_KEY} etc.)
kubectl -n project-bot create configmap project-bot-config \
  --from-file=config.yaml=./config.yaml \
  --from-file=suitability_rules.yaml=./local-profile/suitability_rules.yaml \
  --from-file=search_group_filters.yaml=./local-profile/search_group_filters.yaml

# Eigene Markdown-Kompetenzprofile (gesamter Ordner)
kubectl -n project-bot create configmap project-bot-profiles \
  --from-file=./local-profile/competency_profiles/
```

### 2. PVC, Deployment, Service & optionale `HTTPRoute`

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: project-bot-storage
  namespace: project-bot
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 5Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: project-bot
  namespace: project-bot
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: project-bot
  template:
    metadata:
      labels:
        app: project-bot
    spec:
      containers:
        - name: project-bot
          image: ghcr.io/<dein-user>/project-bot:latest
          ports:
            - name: http
              containerPort: 8002
          env:
            - name: TZ
              value: Europe/Berlin
            - name: FLASK_ENV
              value: production
            - name: FLASK_HOST
              value: "0.0.0.0"
            - name: FLASK_PORT
              value: "8002"
            - name: LOG_FILE
              value: /app/logs/app.log
          envFrom:
            - secretRef:
                name: project-bot-secrets
          volumeMounts:
            # Persistente Laufzeitdaten auf einem PVC (getrennte subPaths)
            - name: storage
              mountPath: /app/data
              subPath: data
            - name: storage
              mountPath: /app/projects
              subPath: projects
            - name: storage
              mountPath: /app/logs
              subPath: logs
            # Persönliche Konfiguration & Regeln (read-only)
            - name: config
              mountPath: /app/config.yaml
              subPath: config.yaml
              readOnly: true
            - name: config
              mountPath: /app/suitability_rules.yaml
              subPath: suitability_rules.yaml
              readOnly: true
            - name: config
              mountPath: /app/search_group_filters.yaml
              subPath: search_group_filters.yaml
              readOnly: true
            - name: profiles
              mountPath: /app/competency_profiles
              readOnly: true
          readinessProbe:
            httpGet:
              path: /api/v1/health
              port: http
            initialDelaySeconds: 10
            periodSeconds: 15
          livenessProbe:
            httpGet:
              path: /api/v1/health
              port: http
            initialDelaySeconds: 20
            periodSeconds: 30
      volumes:
        - name: storage
          persistentVolumeClaim:
            claimName: project-bot-storage
        - name: config
          configMap:
            name: project-bot-config
        - name: profiles
          configMap:
            name: project-bot-profiles
---
apiVersion: v1
kind: Service
metadata:
  name: project-bot
  namespace: project-bot
spec:
  selector:
    app: project-bot
  ports:
    - name: http
      port: 8002
      targetPort: http
---
# Optional: Gateway API HTTPRoute (z. B. mit ArgoCD / ServerSideApply)
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: project-bot
  namespace: project-bot
spec:
  parentRefs:
    - name: main-gateway
      namespace: kube-system
  hostnames:
    - project-bot.example.local
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - group: ''
          kind: Service
          name: project-bot
          port: 8002
          weight: 1
```

> **Hinweis zu `subPath`-Mounts in Kubernetes:** Änderungen an `ConfigMap`s, die per `subPath` gemountet sind (`config.yaml`, `suitability_rules.yaml`, `search_group_filters.yaml`), werden von Kubernetes erst nach einem Pod-Neustart (`kubectl -n project-bot rollout restart deploy/project-bot`) übernommen.
