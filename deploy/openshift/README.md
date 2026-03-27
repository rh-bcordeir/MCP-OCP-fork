# Deploy remediation stack on OpenShift

Two workloads in the **same namespace**, wired with **ClusterIP Services**:

| Component           | Service           | Port | Role                                                      |
| ------------------- | ----------------- | ---- | --------------------------------------------------------- |
| **agent-console**   | `agent-console`   | 8080 | Nginx: static UI + reverse-proxy `/api/remediation` → API |
| **remediation-api** | `remediation-api` | 8787 | FastAPI + SSE + in-cluster Kubernetes client              |

Browsers use a **single OpenShift Route** to **agent-console** only. The UI calls **relative** `/api/remediation/...`, so nginx forwards to `http://remediation-api:8787` (in-cluster DNS). **No CORS** is required for that path.

## Prerequisites

- `oc` / `kubectl` and permission to create **ClusterRole** + **ClusterRoleBinding** (cluster admin or equivalent).
- **No public registry required**: manifests use the **OpenShift internal registry** in your project. Populate images with **`oc start-build`** (binary build) or **Podman** + push.

## RBAC scope (important)

The remediation workflow lists pods **cluster-wide** (`list_pod_for_all_namespaces`). The bundled **ClusterRole** grants:

- `pods`: get, list, watch (all namespaces)
- `pods/log`: get
- `deployments`: get, list, patch, update (all namespaces)

This is **broad**. To reduce blast radius you would need **code changes** to list only allowed namespaces and a **Role** per namespace instead of a ClusterRole.

## Ansible automation

From [`deploy/ansible/`](../ansible/README.md):

```bash
cd deploy/ansible
ansible-playbook playbooks/deploy.yml
```

## Images (why not `docker.io/...:latest`?)

Bare names like `agent-console:latest` resolve to **Docker Hub** (`docker.io/library/...`), which is wrong for this app and often returns **access denied**. The Deployments in this repo use the **cluster internal registry**:

`image-registry.openshift-image-registry.svc:5000/<namespace>/remediation-api:latest`  
`image-registry.openshift-image-registry.svc:5000/<namespace>/agent-console:latest`

You must **build or push** those tags into your namespace’s **ImageStreams** before pods will start.

### Option A — On-cluster builds (no Quay, no local Podman)

Manifests include **ImageStreams** + **binary Docker BuildConfigs** ([27-buildconfig-remediation-api.yaml](27-buildconfig-remediation-api.yaml), [28-buildconfig-agent-console.yaml](28-buildconfig-agent-console.yaml)). From the **repository root**:

```bash
cd /path/to/basic-mcp
oc project remediation-app   # or your namespace

oc start-build remediation-api --from-dir=. --follow
oc start-build agent-console --from-dir=. --follow
```

`--from-dir` uploads the directory; keep it lean (no huge `node_modules` if you can avoid it — the Dockerfiles run `npm ci` / `pip` inside the build).

**Ansible:** after apply, run with `-e remediation_ocp_build=true` to execute the same `oc start-build` steps (see [`deploy/ansible/README.md`](../ansible/README.md)).

### Option B — Podman locally, push to internal registry

```bash
cd /path/to/basic-mcp

podman build -f deploy/openshift/Dockerfile.remediation-api -t remediation-api:latest .
podman build -f deploy/openshift/Dockerfile.agent-console -t agent-console:latest .

oc registry login
# push to image-registry.../remediation-app/...
```

Then ensure Deployment image names match what you pushed (same internal-registry pullspec as in the YAML).

## Install

1. Create project (or apply namespace manifest):

   ```bash
   oc new-project remediation-app
   # or: oc apply -f deploy/openshift/00-namespace.yaml
   ```

2. Apply manifests (numeric order in filenames; or apply the whole directory):

   ```bash
   oc apply -f deploy/openshift/
   ```

   If you already created the project with `oc new-project`, you can skip or ignore `00-namespace.yaml`.

3. **Build images** (Option A or B above). Deployments already reference the internal registry; no `docker.io` pull.

## Verify

```bash
# API from another pod in the namespace
oc run curl --rm -it --restart=Never --image=curlimages/curl -- \
  curl -sS http://remediation-api.remediation-app.svc:8787/api/remediation/health

# Route URL (UI)
oc get route agent-console -n remediation-app -o jsonpath='{.spec.host}{"\n"}'
```

Open the Route in a browser → **Auto Remediate** → confirm SSE log streaming (nginx: `proxy_buffering off`, long `proxy_read_timeout`).

## Where LLM / AI credentials live (OpenShift)

Credentials are **not** stored in the image. Put them in a **Kubernetes Secret** in the **same namespace** as `remediation-api` (e.g. `remediation-app`), then expose them to the pod as **environment variables** via `valueFrom.secretKeyRef`.

The API reads (see `remediation-api/app/services/remediation_runner.py`):

| Variable (any one pair)                 | Purpose                                  |
| --------------------------------------- | ---------------------------------------- |
| `GRANITE_API_BASE` or `OPENAI_BASE_URL` | OpenAI-compatible API base URL           |
| `GRANITE_API_TOKEN` or `OPENAI_API_KEY` | Bearer / API key                         |
| `LLM_MODEL`                             | Optional model id (default `granite-8b`) |

**1. Create the Secret** (pick one approach):

```bash
oc create secret generic remediation-llm-credentials \
  --from-literal=GRANITE_API_BASE='https://your-llm.apps.../v1' \
  --from-literal=GRANITE_API_TOKEN='your-token' \
  -n remediation-app
```

Or edit and apply [`examples/llm-credentials-secret.yaml`](examples/llm-credentials-secret.yaml) (replace placeholders; do not commit real secrets).

**2. Reference it on the Deployment** — add under `containers[0].env` in [`31-deployment-remediation-api.yaml`](31-deployment-remediation-api.yaml):

```yaml
- name: GRANITE_API_BASE
  valueFrom:
    secretKeyRef:
      name: remediation-llm-credentials
      key: GRANITE_API_BASE
- name: GRANITE_API_TOKEN
  valueFrom:
    secretKeyRef:
      name: remediation-llm-credentials
      key: GRANITE_API_TOKEN
- name: LLM_MODEL
  value: 'granite-8b'
```

**3. Re-apply** (or patch): `oc apply -f ...` / Ansible deploy.

For GitOps, prefer **Sealed Secrets**, **External Secrets Operator**, or **Vault** instead of plain YAML with `stringData` in git.

**Ansible:** the deploy playbook can create the Secret from extra-vars **or**, if the Secret already exists, only run `oc set env … --from=secret/…` with `-e remediation_llm_from_existing_secret=true` (see [`deploy/ansible/README.md`](../ansible/README.md)).

## Optional: LLM env suggestions

Same variables as above; only the **storage** changes (Secret vs SealedSecret / ESO).

## Dual-Route topology (not recommended)

If the UI is built with `VITE_REMEDIATION_API=https://...` pointing at a **second** Route for the API, set on **remediation-api**:

```yaml
env:
  - name: REMEDIATION_CORS_ORIGINS
    value: 'https://<agent-console-route-host>'
```

## Troubleshooting

| Symptom                                                      | Check                                                                                                                                           |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| 502 / SSE cuts off                                           | nginx `proxy_read_timeout`, API pod logs, Route idle timeout                                                                                    |
| API `Forbidden` on pods                                      | ClusterRoleBinding + ServiceAccount on `remediation-api` Deployment                                                                             |
| Image pull errors / `docker.io/... denied`                   | Do not use bare `:latest` on Docker Hub; build with `oc start-build` or push to internal registry; see **Images** above                         |
| Permission denied in container                               | OpenShift SCC; images use non-root UIDs (8080 nginx, 1001 API)                                                                                  |
| Build: `chgrp` / `chmod … Operation not permitted` on `/app` | Rootless cluster builds often block changing perms on context layers — use `COPY --chmod=…` (see Dockerfiles; needs Buildah 1.23+ / recent OCP) |

## Files

- [Dockerfile.remediation-api](Dockerfile.remediation-api) — `registry.access.redhat.com/ubi9/python-312`, copies `openshift_tool_handlers.py`, `remediation_workflow.py`, `remediation-api/`
- [Dockerfile.agent-console](Dockerfile.agent-console) — `ubi9/nodejs-22` (build) + `ubi9/nginx-124` (runtime); no docker.io images
- [nginx.conf](nginx.conf) — SPA `try_files`, `/api/remediation` proxy + SSE-friendly settings
