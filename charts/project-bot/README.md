# Project Bot Helm Chart

Deploys **Project Bot** on Kubernetes with:
- Non-root container security context (`UID/GID 1000`, `fsGroup: 1000`, `drop: [ALL]`)
- Persistent storage (`PVC`) for `/app/data`, `/app/projects`, `/app/logs` (`Recreate` deployment strategy)
- Inline or pre-existing `ConfigMap`s for `config.yaml`, `suitability_rules.yaml`, `search_group_filters.yaml`, and `competency_profiles/*.md` (with automatic Pod rollout checksums on changes)
- Inline or pre-existing `Secret` for API keys & IMAP credentials
- Optional Gateway API `HTTPRoute` (with explicit `backendRefs` defaults for ArgoCD SSA)

## Install from GHCR (OCI)

```bash
helm upgrade --install project-bot oci://ghcr.io/dmuix/charts/project-bot \
  --namespace project-bot --create-namespace \
  -f my-private-values.yaml
```

## Public vs. Private Values (GitOps)

When deploying with ArgoCD multi-source (public `apps` repo + private `live` repo + OpenBao/VSO):

- **Public apps repo:** chart version, pinned `image.digest`, `existingSecret: project-bot-secrets`, `httpRoute.enabled: false` (when fronting with an OIDC proxy), and OIDC/RBAC wiring.
- **Private live repo (`values.yml`):** personal `customConfig` (`configYaml`, `suitabilityRulesYaml`, `searchGroupFiltersYaml`, `profiles`) and private `hostnames` / OIDC URLs.
- **OpenBao / VSO (`VaultStaticSecret` → `project-bot-secrets`):** API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`) and IMAP/provider credentials — never committed to Git.

## Example `my-private-values.yaml` (keep outside public Git)

```yaml
image:
  # Pin by immutable digest (takes precedence over tag)
  digest: "sha256:0123456789abcdef..."

existingSecret: project-bot-secrets  # e.g. synced via OpenBao / VSO VaultStaticSecret

customConfig:
  configYaml: |
    llm:
      provider: OpenAI
      model: gpt-5-mini
      api_key: ${OPENAI_API_KEY}
  suitabilityRulesYaml: |
    suitability:
      thresholds:
        high: 50
        medium: 25
  searchGroupFiltersYaml: |
    search_group_filters: {}
  profiles:
    devops_kubernetes.md: |
      # DevOps & Kubernetes Profile
      - Kubernetes, Helm, ArgoCD, Terraform, Linux

httpRoute:
  enabled: true
  parentRefs:
    - name: main-gateway
      namespace: kube-system
  hostnames:
    - project-bot.example.local
```
