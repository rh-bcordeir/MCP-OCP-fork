"""
OpenShift / Kubernetes MCP tool implementations (plain Python functions).

Used by:
  - server-gpt.py (FastMCP registers these as MCP tools over stdio)
  - remediation-api in-process executor (no subprocess, no uv run)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from kubernetes import client, config
from kubernetes.client import ApiException

# Constants
OPENSHIFT_GROUP = "config.openshift.io"
OPENSHIFT_VERSION = "v1"
CLUSTERVERSION_PLURAL = "clusterversions"
CLUSTERVERSION_NAME = "version"

# Cache for auth loading
_auth_loaded = False


def _load_kube_auth() -> None:
    global _auth_loaded
    if _auth_loaded:
        return

    try:
        config.load_incluster_config()
    except Exception:
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


def _get_cluster_version_obj() -> Dict[str, Any]:
    return _custom().get_cluster_custom_object(
        group=OPENSHIFT_GROUP,
        version=OPENSHIFT_VERSION,
        plural=CLUSTERVERSION_PLURAL,
        name=CLUSTERVERSION_NAME,
    )


def _channel_from_version(version: str) -> str:
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


_ERROR_PHASES = {"Failed", "Error", "Unknown"}
_WAITING_ERROR_REASONS = {
    "CrashLoopBackOff", "Error", "ImagePullBackOff", "ErrImagePull",
    "CreateContainerError", "CreateContainerConfigError", "InvalidImageName",
}

_PROBLEM_POD_STATUS_RE = re.compile(
    r"Error|CrashLoop|ImagePull|ErrImage|Pending|Evicted|OOMKilled|CreateContainer",
    re.IGNORECASE,
)


def _is_platform_namespace_for_listing(ns: str, *, include_openshift_namespaces: bool) -> bool:
    """
    True → skip pod (platform / non-application namespaces).

    Matches remediation_workflow app-only policy: default, kube-*, openshift-* (unless flag).
    """
    if not ns:
        return True
    if ns == "default":
        return True
    if ns.startswith("kube-"):
        return True
    if not include_openshift_namespaces and ns.startswith("openshift-"):
        return True
    return False


def _pod_has_errors(pod: client.V1Pod) -> bool:
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


def _pod_status_text_for_grep(pod: client.V1Pod) -> str:
    chunks: List[str] = []
    if pod.status:
        if pod.status.phase:
            chunks.append(pod.status.phase)
        if pod.status.reason:
            chunks.append(pod.status.reason)
        for cs in (pod.status.init_container_statuses or []) + (pod.status.container_statuses or []):
            st = cs.state
            if not st:
                continue
            if st.waiting and st.waiting.reason:
                chunks.append(st.waiting.reason)
            if st.terminated and st.terminated.reason:
                chunks.append(st.terminated.reason)
    return " ".join(chunks)


def _pod_matches_oc_problem_grep(pod: client.V1Pod) -> bool:
    text = _pod_status_text_for_grep(pod)
    if _PROBLEM_POD_STATUS_RE.search(text):
        return True
    for cs in (pod.status.container_statuses or []) if pod.status else []:
        st = cs.state
        if st and st.waiting and st.waiting.reason in _WAITING_ERROR_REASONS:
            return True
    return False


def _list_all_pods_all_namespaces(v1: client.CoreV1Api) -> List[client.V1Pod]:
    all_items: List[client.V1Pod] = []
    _continue: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"limit": 500}
        if _continue:
            kwargs["_continue"] = _continue
        resp = v1.list_pod_for_all_namespaces(**kwargs)
        all_items.extend(resp.items or [])
        meta = resp.metadata
        _continue = getattr(meta, "_continue", None) if meta is not None else None
        if not _continue:
            break
    return all_items


def _pod_problem_status_summary(pod: client.V1Pod) -> str:
    if not pod.status:
        return "Unknown"
    if pod.status.reason:
        return pod.status.reason
    for cs in (pod.status.init_container_statuses or []) + (pod.status.container_statuses or []):
        st = cs.state
        if st and st.waiting and st.waiting.reason:
            return st.waiting.reason
        if st and st.terminated and st.terminated.reason:
            return st.terminated.reason
    return pod.status.phase or "Unknown"


# --- Public tool handlers (MCP tool names match function names) ---


def verificar_status_sistema(componente: str) -> str:
    """Checks basic status for a component."""
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


def listar_nodes() -> str:
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


def listar_pods(namespace: str) -> str:
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


def listar_pods_em_erro_cluster(include_openshift_namespaces: bool = False) -> str:
    """
    List problem-state pods cluster-wide.

    By default excludes ``default``, ``kube-*``, and ``openshift-*`` (OpenShift-managed infra).
    Pass ``include_openshift_namespaces=True`` to include openshift-*; kube-* and default stay excluded.
    """
    try:
        v1 = _core_v1()
        pods = _list_all_pods_all_namespaces(v1)
        out: List[str] = []
        for p in pods:
            if not _pod_matches_oc_problem_grep(p):
                continue
            ns = p.metadata.namespace or "?"
            if _is_platform_namespace_for_listing(ns, include_openshift_namespaces=include_openshift_namespaces):
                continue
            name = p.metadata.name or "?"
            phase = p.status.phase if p.status else "?"
            summary = _pod_problem_status_summary(p)
            restarts = 0
            for c in (p.status.container_statuses or []) if p.status else []:
                restarts += c.restart_count or 0
            out.append(
                f"- {ns}/{name} | Status={summary} | Phase={phase} | Restarts={restarts}"
            )
        if not out:
            logger.info("listar_pods_em_erro_cluster: no problem pods matched")
            scope = (
                "application namespaces (excludes default, kube-*, openshift-*)"
                if not include_openshift_namespaces
                else "application + openshift-* (still excludes default, kube-*)"
            )
            return (
                f"No pods matched problem filter in {scope} "
                "(Error, CrashLoop, ImagePull, ErrImage, Pending, Evicted, OOMKilled, CreateContainer). "
                "Use include_openshift_namespaces=true to include openshift-* namespaces."
            )
        scope = (
            "application namespaces (excludes default, kube-*, openshift-*)"
            if not include_openshift_namespaces
            else "namespaces including openshift-* (excludes default, kube-*)"
        )
        header = f"Found {len(out)} pod(s) matching problem status filter ({scope}):\n"
        logger.info("listar_pods_em_erro_cluster: returning %d problem pod(s)", len(out))
        return header + "\n".join(out)
    except ApiException as e:
        return f"Failed to list pods cluster-wide: {e.status} {e.reason} - {e.body}"
    except Exception as e:
        return f"Failed to list pods cluster-wide: {type(e).__name__}: {e}"


def iniciar_upgrade_openshift(version: str, image: Optional[str] = None) -> str:
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


def ver_logs_pod(
    pod: str,
    namespace: str,
    container: Optional[str] = None,
    tail_lines: Optional[int] = 100,
    timestamps: bool = False,
) -> str:
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


def definir_env_deployment(
    deployment: str,
    namespace: str,
    env_vars: List[Dict[str, str]],
) -> str:
    if not env_vars:
        return "No env vars provided. Nothing to do."

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


# Map MCP tool name -> handler (for in-process dispatch)
MCP_TOOL_DISPATCH: Dict[str, Any] = {
    "verificar_status_sistema": verificar_status_sistema,
    "listar_nodes": listar_nodes,
    "listar_pods": listar_pods,
    "listar_pods_em_erro_cluster": listar_pods_em_erro_cluster,
    "iniciar_upgrade_openshift": iniciar_upgrade_openshift,
    "ver_logs_pod": ver_logs_pod,
    "definir_env_deployment": definir_env_deployment,
}
