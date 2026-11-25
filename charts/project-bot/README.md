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

## Example `my-private-values.yaml` (keep outside public Git)

```yaml
existingSecret: project-bot-secrets  # or use `secrets: { OPENAI_API_KEY: "..." }`

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
