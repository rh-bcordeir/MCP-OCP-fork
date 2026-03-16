from __future__ import annotations

from typing import Any, Dict, Optional, List

from mcp.server.fastmcp import FastMCP

from kubernetes import client, config
from kubernetes.client import ApiException

# Constants
OPENSHIFT_GROUP = "config.openshift.io"
OPENSHIFT_VERSION = "v1"
CLUSTERVERSION_PLURAL = "clusterversions"
CLUSTERVERSION_NAME = "version"

mcp = FastMCP("DemoOpenShift")

# Cache for auth loading
_auth_loaded = False


# -----------------------------
# Kubernetes/OpenShift API setup
# -----------------------------
def _load_kube_auth() -> None:
    """
    Tries in-cluster auth first (ServiceAccount), then falls back to local kubeconfig.
    Caches the result to avoid repeated loading.
    """
    global _auth_loaded
    if _auth_loaded:
        return
    
    try:
        config.load_incluster_config()
    except Exception:
        # Uses default kubeconfig resolution (~/.kube/config) or KUBECONFIG env var
        config.load_kube_config()
    finally:
        _auth_loaded = True


def _core_v1() -> client.CoreV1Api:
    _load_kube_auth()
    return client.CoreV1Api()


def _custom() -> client.CustomObjectsApi:
    _load_kube_auth()
    return client.CustomObjectsApi()


def _apps_v1() -> client.AppsV1Api:
    _load_kube_auth()
    return client.AppsV1Api()


# -----------------------------
# OpenShift helpers
# -----------------------------
def _get_cluster_version_obj() -> Dict[str, Any]:
    """
    OpenShift ClusterVersion is cluster-scoped.
    """
    return _custom().get_cluster_custom_object(
        group=OPENSHIFT_GROUP,
        version=OPENSHIFT_VERSION,
        plural=CLUSTERVERSION_PLURAL,
        name=CLUSTERVERSION_NAME,
    )


def _channel_from_version(version: str) -> str:
    """
    Derives the stable channel name from a semantic version (e.g. '4.19.24' -> 'stable-4.19').
    """
    parts = version.strip().split(".")
    if len(parts) >= 2:
        return f"stable-{parts[0]}.{parts[1]}"
    return f"stable-{version}"


def _summarize_clusterversion(cv: Dict[str, Any]) -> str:
    status = cv.get("status", {})
    desired = cv.get("spec", {}).get("desiredUpdate", {}) or {}
    history = status.get("history", []) or []
    conds = {c.get("type"): c for c in (status.get("conditions") or [])}

    current = status.get("desired", {}).get("version") or status.get("version")
    desired_ver = desired.get("version")
    progressing = conds.get("Progressing", {}).get("status")
    available = conds.get("Available", {}).get("status")
    failing = conds.get("Failing", {}).get("status")

    # last history entry usually has useful info
    last_hist = history[0] if history else {}
    state = last_hist.get("state")
    started = last_hist.get("startedTime")
    completed = last_hist.get("completionTime")
    msg = conds.get("Progressing", {}).get("message") or conds.get("Failing", {}).get("message") or ""

    lines = [
        f"Current version: {current or 'unknown'}",
        f"Desired version: {desired_ver or current or 'unknown'}",
        f"Available: {available} | Progressing: {progressing} | Failing: {failing}",
        f"Last update state: {state or 'unknown'}",
    ]
    if started:
        lines.append(f"Started: {started}")
    if completed:
        lines.append(f"Completed: {completed}")
    if msg:
        lines.append(f"Message: {msg}")

    return "\n".join(lines)


# -----------------------------
# Pod helpers
# -----------------------------
_ERROR_PHASES = {"Failed", "Error", "Unknown"}
_WAITING_ERROR_REASONS = {
    "CrashLoopBackOff", "Error", "ImagePullBackOff", "ErrImagePull",
    "CreateContainerError", "CreateContainerConfigError", "InvalidImageName",
}


def _pod_has_errors(pod: client.V1Pod) -> bool:
    """Returns True if pod shows signs of problems (phase, restarts, container status)."""
    if not pod.status:
        return False
    phase = pod.status.phase or ""
    if phase in _ERROR_PHASES:
        return True
    restart_count = 0
    for cs in (pod.status.container_statuses or []) if pod.status else []:
        restart_count += getattr(cs, "restart_count", 0) or 0
        if not getattr(cs, "ready", True):
            return True
        state = cs.state
        if state:
            if state.waiting:
                reason = (state.waiting.reason or "").strip()
                if reason in _WAITING_ERROR_REASONS:
                    return True
            if state.terminated:
                exit_code = getattr(state.terminated, "exit_code", None)
                if exit_code is not None and exit_code != 0:
                    return True
    return restart_count > 0


# -----------------------------
# MCP tools
# -----------------------------
@mcp.tool()
def verificar_status_sistema(componente: str) -> str:
    """
    Checks basic status for a component. Useful before updates.
    Args:
        componente: 'cluster' | 'api' | 'nos'
    """
    c = componente.lower().strip()

    if c == "cluster":
        try:
            cv = _get_cluster_version_obj()
            return _summarize_clusterversion(cv)
        except ApiException as e:
            return f"Failed to read ClusterVersion: {e.status} {e.reason} - {e.body}"
        except Exception as e:
            return f"Failed to read ClusterVersion: {type(e).__name__}: {e}"

    if c == "api":
        # Quick ping: list namespaces (cheap)
        try:
            v1 = _core_v1()
            v1.list_namespace(limit=1)
            return "API: Online (basic request succeeded)."
        except Exception as e:
            return f"API: Unreachable or unauthorized: {type(e).__name__}: {e}"

    if c in ("nos", "nodes"):
        try:
            v1 = _core_v1()
            nodes = v1.list_node()
            ready = 0
            for n in nodes.items:
                for cond in n.status.conditions or []:
                    if cond.type == "Ready" and cond.status == "True":
                        ready += 1
                        break
            return f"Nodes: {ready}/{len(nodes.items)} Ready."
        except Exception as e:
            return f"Nodes: Failed to list: {type(e).__name__}: {e}"

    return f"Componente '{componente}' desconhecido."


@mcp.tool()
def listar_nodes() -> str:
    """
    Lists cluster nodes with basic info (name, ready, kubelet version).
    """
    try:
        v1 = _core_v1()
        nodes = v1.list_node().items
        out: List[str] = []
        for n in nodes:
            name = n.metadata.name
            kubelet = n.status.node_info.kubelet_version if n.status and n.status.node_info else "unknown"
            ready = "Unknown"
            for cond in (n.status.conditions or []):
                if cond.type == "Ready":
                    ready = cond.status
                    break
            out.append(f"- {name} | Ready={ready} | kubelet={kubelet}")
        return "\n".join(out) if out else "No nodes returned."
    except ApiException as e:
        return f"Failed to list nodes: {e.status} {e.reason} - {e.body}"
    except Exception as e:
        return f"Failed to list nodes: {type(e).__name__}: {e}"


@mcp.tool()
def listar_pods(namespace: str) -> str:
    """
    Lists pods in a given namespace with basic info (name, status, restarts, age).
    Pods with errors (Failed phase, restarts, CrashLoopBackOff, etc.) are marked with
    '| HasErrors=True'. Filter that field if you need only problematic pods.
    Args:
        namespace: The Kubernetes namespace to list pods from (e.g. 'default', 'openshift-ingress')
    """
    try:
        v1 = _core_v1()
        pods = v1.list_namespaced_pod(namespace=namespace).items
        out: List[str] = []
        for p in pods:
            name = p.metadata.name
            phase = p.status.phase if p.status else "Unknown"
            restart_count = 0
            for c in (p.status.container_statuses or []) if p.status else []:
                restart_count += c.restart_count
            age = p.metadata.creation_timestamp
            age_str = str(age) if age else "unknown"
            has_errors = _pod_has_errors(p)
            line = f"- {name} | Phase={phase} | Restarts={restart_count} | Created={age_str}"
            if has_errors:
                line += " | HasErrors=True"
            out.append(line)
        return "\n".join(out) if out else f"No pods found in namespace '{namespace}'."
    except ApiException as e:
        return f"Failed to list pods in '{namespace}': {e.status} {e.reason} - {e.body}"
    except Exception as e:
        return f"Failed to list pods in '{namespace}': {type(e).__name__}: {e}"


@mcp.tool()
def iniciar_upgrade_openshift(version: str, image: Optional[str] = None) -> str:
    """
    Starts an OpenShift cluster upgrade by setting ClusterVersion.spec.channel and
    spec.desiredUpdate. The channel is derived from the target version (e.g. 4.19.24 -> stable-4.19).
    Args:
        version: target version (example: '4.14.25')
        image: optional release image pullspec (advanced; usually omit)
    Notes:
        Requires RBAC permission to patch clusterversions.config.openshift.io 'version'.
    """
    channel = _channel_from_version(version)
    body: Dict[str, Any] = {
        "spec": {
            "channel": channel,
            "desiredUpdate": {"version": version},
        }
    }
    if image:
        body["spec"]["desiredUpdate"]["image"] = image

    try:
        _custom().patch_cluster_custom_object(
            group=OPENSHIFT_GROUP,
            version=OPENSHIFT_VERSION,
            plural=CLUSTERVERSION_PLURAL,
            name=CLUSTERVERSION_NAME,
            body=body,
        )
        cv = _get_cluster_version_obj()
        return "Upgrade request submitted.\n\n" + _summarize_clusterversion(cv)
    except ApiException as e:
        return f"Failed to patch ClusterVersion: {e.status} {e.reason} - {e.body}"
    except Exception as e:
        return f"Failed to patch ClusterVersion: {type(e).__name__}: {e}"


@mcp.tool()
def ver_logs_pod(
    pod: str,
    namespace: str,
    container: Optional[str] = None,
    tail_lines: Optional[int] = 100,
    timestamps: bool = False,
) -> str:
    """
    Retrieves the logs of a pod in the cluster.
    Args:
        pod: Name of the pod.
        namespace: Kubernetes namespace containing the pod.
        container: Optional container name (required if pod has multiple containers).
        tail_lines: Number of lines to retrieve from the end of the log (default: 100).
        timestamps: Include timestamps in each log line (default: False).
    """
    try:
        v1 = _core_v1()
        logs = v1.read_namespaced_pod_log(
            name=pod,
            namespace=namespace,
            container=container,
            tail_lines=tail_lines,
            timestamps=timestamps,
        )
        if not logs:
            return f"No logs returned for pod '{pod}' in namespace '{namespace}'."
        return logs
    except ApiException as e:
        return f"Failed to get logs for pod '{pod}' in '{namespace}': {e.status} {e.reason} - {e.body}"
    except Exception as e:
        return f"Failed to get logs: {type(e).__name__}: {e}"


@mcp.tool()
def definir_env_deployment(
    deployment: str,
    namespace: str,
    env_vars: List[Dict[str, str]],
) -> str:
    """
    Sets environment variables on a Deployment. Merges with existing env vars
    (overwrites any with the same name). Applies to all containers in the deployment.
    Args:
        deployment: Name of the Deployment.
        namespace: Kubernetes namespace containing the Deployment.
        env_vars: List of env vars to set, each with 'name' and 'value' keys.
                  Example: [{"name": "LOG_LEVEL", "value": "debug"}, {"name": "PORT", "value": "8080"}]
    """
    if not env_vars:
        return "No env vars provided. Nothing to do."

    # Validate and build V1EnvVar list
    env_objs: List[client.V1EnvVar] = []
    for ev in env_vars:
        if not isinstance(ev, dict):
            return f"Invalid env var entry (expected dict): {ev}"
        name = ev.get("name")
        value = ev.get("value")
        if not name:
            return f"Env var entry missing 'name' key: {ev}"
        env_objs.append(client.V1EnvVar(name=name, value=str(value) if value is not None else ""))

    try:
        apps = _apps_v1()
        dep = apps.read_namespaced_deployment(name=deployment, namespace=namespace)

        if not dep.spec or not dep.spec.template or not dep.spec.template.spec or not dep.spec.template.spec.containers:
            return f"Deployment '{deployment}' has no containers."

        # Build merged env: existing by name, then overwrite/add from env_vars
        env_by_name: Dict[str, client.V1EnvVar] = {}
        for c in dep.spec.template.spec.containers:
            for e in (c.env or []):
                if e.name:
                    env_by_name[e.name] = e
        for e in env_objs:
            env_by_name[e.name] = e

        merged = list(env_by_name.values())

        for c in dep.spec.template.spec.containers:
            c.env = merged

        apps.patch_namespaced_deployment(
            name=deployment,
            namespace=namespace,
            body=dep,
        )
        names_set = ", ".join(e.name for e in env_objs)
        return f"Env vars set on Deployment '{deployment}' in namespace '{namespace}': {names_set}"
    except ApiException as e:
        return f"Failed to set env vars: {e.status} {e.reason} - {e.body}"
    except Exception as e:
        return f"Failed to set env vars: {type(e).__name__}: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
