# Ansible: deploy OpenShift stack

Applies all YAML manifests from [`../openshift/`](../openshift/) in order, optionally substitutes the project name, patches container images, and can build images with Podman.

## Requirements

- Ansible 2.14+ (Core)
- `oc` **or** `kubectl`, with a logged-in context (`oc login` / `KUBECONFIG`) pointing at **your OpenShift cluster**

**Why `hosts: localhost`?** The playbook runs on the **control node** (your machine or a jump host). It does **not** install Ansible *on* OpenShift. All resources are created **on the cluster** through the Kubernetes/OpenShift API. Ensure `oc whoami` / `kubectl config current-context` targets the right cluster before running.

- **No extra Python packages**: manifests are applied with **`oc apply`** / **`kubectl apply`**, so you do **not** need `pip install kubernetes` on the control host.
- **OpenShift** for `Route` and `oc` (plain Kubernetes may lack `route.openshift.io` — apply manifests with `kubectl` only if you replace the Route or use Ingress).

## Deploy

From this directory:

```bash
cd deploy/ansible
ansible-playbook playbooks/deploy.yml
```

**LLM / AI credentials (optional):** if you pass both base URL and token, the playbook creates/updates a Secret and runs `oc set env deployment/remediation-api --from=secret/…` (no secrets in git):

```bash
ansible-playbook playbooks/deploy.yml \
  -e remediation_llm_granite_base='https://your-llm-route.../v1' \
  -e remediation_llm_granite_token='your-token' \
  -e remediation_llm_model='granite-8b'
```

Or OpenAI-style names: `remediation_llm_openai_base` + `remediation_llm_openai_key` (used only if Granite pair is not set). Disable wiring: `-e remediation_llm_wire_deployment=false`. See [`deploy/openshift/README.md`](../openshift/README.md) and [`examples/llm-credentials-secret.yaml`](../openshift/examples/llm-credentials-secret.yaml).

**Secret already created** (e.g. `GRANITE_API_BASE` / `GRANITE_API_TOKEN` are already in the cluster): do **not** pass base/token to Ansible. Only attach that Secret to `remediation-api`:

```bash
ansible-playbook playbooks/deploy.yml \
  -e remediation_llm_from_existing_secret=true \
  -e remediation_llm_secret_name=remediation-llm-credentials
```

Adjust `remediation_llm_secret_name` if your Secret has another name. Optional model: `-e remediation_llm_model=granite-8b`.

**If the Secret doesn't exist**, the playbook will fail with a helpful error listing available secrets. Create it first:

```bash
oc create secret generic remediation-llm-credentials \
  --from-literal=GRANITE_API_BASE='https://your-llm-route.../v1' \
  --from-literal=GRANITE_API_TOKEN='your-token' \
  -n remediation-app
```

Or let Ansible create it by passing the credentials as extra-vars (see above).

### Does `remediation-api` actually have the Secret as env?

The checked-in Deployment YAML only has `REMEDIATION_PROJECT_ROOT`; LLM keys are **not** in git unless you uncomment `secretKeyRef` in `31-deployment-remediation-api.yaml`. When Ansible runs `oc set env deployment/remediation-api --from=secret/…`, OpenShift **patches the live Deployment** so the pod spec includes `valueFrom.secretKeyRef` for each key in that Secret.

Verify on the cluster:

```bash
oc get deployment remediation-api -n remediation-app -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}{"\t"}{.valueFrom.secretKeyRef.name}{"/"}{.valueFrom.secretKeyRef.key}{"\n"}{end}'
```

You should see lines like `GRANITE_API_BASE    remediation-llm-credentials/GRANITE_API_BASE` (empty lines are plain `value:` env vars). If there is no `secretKeyRef` output for Granite/OpenAI keys, the wire step did not run or failed — re-run with the LLM extra-vars above.

**Rollout “progress deadline exceeded”** usually means the pod is **CrashLoop**, **ImagePullBackOff**, or failing probes — not necessarily missing Secret. Check: `oc get pods -n remediation-app`, `oc logs deployment/remediation-api -n remediation-app`, `oc describe pod -l app.kubernetes.io/name=remediation-api -n remediation-app`.

**Start on-cluster image builds with Ansible** (runs `oc start-build … --from-dir` for both apps after manifests are applied, before rollout):

```bash
ansible-playbook playbooks/deploy.yml -e remediation_ocp_build=true
```

Equivalent to setting `remediation_ocp_build: true` in [`group_vars/all.yml`](group_vars/all.yml). It stays `false` by default so normal deploys don’t upload the whole repo every time.

From another working directory, set `ANSIBLE_CONFIG` so inventory and `group_vars` load, or rely on playbook defaults (namespace/images are duplicated in the playbook `vars:`):

```bash
ANSIBLE_CONFIG=/path/to/basic-mcp/deploy/ansible/ansible.cfg \
  ansible-playbook /path/to/basic-mcp/deploy/ansible/playbooks/deploy.yml
```

Override project and images:

```bash
ansible-playbook playbooks/deploy.yml \
  -e remediation_namespace=my-team-remediation \
  -e remediation_api_image=image-registry.openshift-image-registry.svc:5000/my-team-remediation/remediation-api:latest \
  -e agent_console_image=image-registry.openshift-image-registry.svc:5000/my-team-remediation/agent-console:latest
```

Skip `oc set image` (use image names already in the YAML):

```bash
ansible-playbook playbooks/deploy.yml -e remediation_set_images=false
```

## Build on OpenShift (internal registry, no Quay)

After manifests are applied, populate **ImageStreams** with binary builds from your repo (same as `oc start-build --from-dir`):

```bash
ansible-playbook playbooks/deploy.yml -e remediation_ocp_build=true
```

Use this on **first deploy** or when Dockerfiles change. Default is `remediation_ocp_build: false` so routine applies do not upload the whole tree.

## Build images (Podman)

Runs from the **repository root** (`basic-mcp/`) using `deploy/openshift/Dockerfile.*`.

```bash
ansible-playbook playbooks/deploy.yml --tags build
```

Then push images to your registry and deploy (or use OpenShift internal registry + `oc import-image`).

**Note:** `--tags build` runs **only** the build tasks, not the cluster apply. Run a full deploy separately:

```bash
ansible-playbook playbooks/deploy.yml --tags build
ansible-playbook playbooks/deploy.yml
```

## What the playbook does

1. Copies [`deploy/openshift/*.yaml`](../openshift/) to `$HOME/.cache/ansible-basic-mcp-ocp/` (or `/tmp/.cache/...` if `HOME` is unset).
2. Replaces the string `remediation-app` with `remediation_namespace` (default `remediation-app`) in every file.
3. Runs `oc apply -f` / `kubectl apply -f` on each staged file (same order as file name prefixes).
4. Optionally runs `oc set image` on both Deployments and waits for rollout.
5. If `remediation_ocp_build: true`, runs `oc start-build` for **remediation-api** and **agent-console** (`--from-dir` repo root) into the project ImageStreams.
6. If LLM extra-vars are set, creates/updates Secret `remediation_llm_secret_name` and optionally `oc set env deployment/remediation-api --from=secret/…`.
7. Prints the **agent-console** Route host when `oc get route` succeeds.

## Configuration files

| File | Purpose |
|------|---------|
| [`ansible.cfg`](ansible.cfg) | Inventory path, stdout YAML |
| [`inventory/hosts.yml`](inventory/hosts.yml) | `localhost` / local connection |
| [`group_vars/all.yml`](group_vars/all.yml) | Default namespace and image names |
| [`requirements.yml`](requirements.yml) | Optional collections (playbook does not require `kubernetes.core`) |

## Troubleshooting

- **`rollout status` / "exceeded its progress deadline" in ~1s**: The Deployment was **already** in a failed rollout state; `rollout status` exits immediately. Run once: `-e remediation_rollout_restart=true` (or `oc rollout restart …`), then re-run the playbook. With `-e remediation_ocp_build=true`, a restart is done automatically after builds.
- **`can't restart paused deployment`**: The Deployment has `spec.paused: true`. Run `oc rollout resume deployment/<name> -n <ns>` before `rollout restart`, or use the current playbook (it runs **resume** before **restart** when using `remediation_rollout_restart` / `remediation_ocp_build`).
- **`Failed to import kubernetes`**: use the current `deploy.yml` (it runs `oc apply`, not `kubernetes.core.k8s`). Or install `pip install kubernetes` if you use an older playbook.
- **Apply / RBAC errors**: `kubectl config current-context` / `oc whoami`, permissions to create ClusterRole and Route.
- **Route apply fails on Kubernetes**: remove or replace `50-route-agent-console.yaml` for Ingress.
- **Image pull errors / Docker Hub denied**: defaults use the **internal registry**; run `-e remediation_ocp_build=true` once or `oc start-build` manually (see `deploy/openshift/README.md`). Override `-e remediation_*_image=` only if you use another registry.
