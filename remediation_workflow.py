"""
CrashLoop remediation orchestration: MCP tool calls via a ToolCaller protocol.

Used by:
  - client-gpt.py (FastMCP stdio Client adapter)
  - remediation-api (in-process OpenShift handlers)

This module does not spawn subprocesses or call uv.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Union

from openai import OpenAI

logger = logging.getLogger(__name__)

TOOL_LIST_PODS_ERROR = "listar_pods_em_erro_cluster"
TOOL_VER_LOGS = "ver_logs_pod"
TOOL_SET_ENV = "definir_env_deployment"

_DEFAULT_ENV_FOR_MISSING: Dict[str, str] = {
    "NAME": "OpenShift",
    "LOG_LEVEL": "info",
    "PORT": "8080",
}

_MISSING_ENV_RE = re.compile(
    r"environment variable\s+['\"]?(\w+)['\"]?\s+is not set",
    re.IGNORECASE,
)
_POD_LINE_RE = re.compile(
    r"^-\s*(?P<ns>[^/\s]+)/(?P<pod>[^\s|]+)\s*\|\s*Status=(?P<status>[^|]+)",
    re.MULTILINE,
)


def _is_default_or_kube_namespace(ns: str) -> bool:
    """Kubernetes default / kube-* — not application workloads."""
    if ns == "default":
        return True
    return ns.startswith("kube-")


def _should_skip_namespace_for_app_only(
    ns: str,
    *,
    include_openshift_namespaces: bool,
) -> bool:
    """
    When remediating app namespaces only, skip platform namespaces.
    OpenShift infra is skipped unless include_openshift_namespaces.
    """
    if _is_default_or_kube_namespace(ns):
        return True
    if not include_openshift_namespaces and ns.startswith("openshift-"):
        return True
    return False

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_object(text: str) -> Dict[str, Any]:
    if not isinstance(text, str):
        raise ValueError("LLM output is not a string")

    cleaned = _JSON_FENCE_RE.sub("", text.strip())

    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"Could not find JSON object in: {text[:200]}...")

    snippet = cleaned[start : end + 1]
    obj = json.loads(snippet)
    if not isinstance(obj, dict):
        raise ValueError("Parsed JSON is not an object")
    return obj


def extract_text(result: Any) -> str:
    """Normalize FastMCP tool results or plain str."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result

    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        first = content[0]
        txt = getattr(first, "text", None)
        if isinstance(txt, str):
            return txt

    return str(result)


def infer_deployment_from_pod_name(pod_name: str) -> str:
    parts = pod_name.split("-")
    if len(parts) >= 3:
        return "-".join(parts[:-2])
    return pod_name


def parse_problem_pod_lines(
    list_output: str,
    *,
    crashloop_only: bool = True,
    include_openshift_namespaces: bool = False,
    namespace_filter: Optional[str] = None,
    pod_filter: Optional[str] = None,
    app_namespaces_only: bool = True,
) -> List[tuple[str, str]]:
    """
    Parse listar_pods_em_erro_cluster output -> [(namespace, pod_name), ...].

    If app_namespaces_only (default True), exclude ``default``, ``kube-*``, and
    ``openshift-*`` (unless include_openshift_namespaces). Pin a namespace with
    namespace_filter to target a specific project regardless.
    """
    targets: List[tuple[str, str]] = []
    for m in _POD_LINE_RE.finditer(list_output):
        ns, pod, status = m.group("ns"), m.group("pod"), m.group("status").strip()
        if namespace_filter and ns != namespace_filter:
            continue
        if pod_filter and pod != pod_filter:
            continue
        if not include_openshift_namespaces and ns.startswith("openshift-"):
            continue
        if app_namespaces_only and not namespace_filter:
            if _should_skip_namespace_for_app_only(
                ns, include_openshift_namespaces=include_openshift_namespaces
            ):
                continue
        if crashloop_only and "crashloop" not in status.lower():
            continue
        targets.append((ns, pod))
    targets.sort(
        key=lambda t: (
            t[0].startswith("openshift-"),
            _is_default_or_kube_namespace(t[0]),
            t[0],
            t[1],
        )
    )
    return targets


def extract_env_fixes_from_logs(logs: str) -> List[Dict[str, str]]:
    seen: set[str] = set()
    out: List[Dict[str, str]] = []
    for m in _MISSING_ENV_RE.finditer(logs):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        value = _DEFAULT_ENV_FOR_MISSING.get(name, "set-by-agent")
        out.append({"name": name, "value": value})
    return out


def suggest_env_fixes_with_llm(
    *,
    openai_client: OpenAI,
    model: str,
    logs: str,
) -> Optional[List[Dict[str, str]]]:
    prompt = """You fix Kubernetes app crashes caused by missing configuration.

Pod/container logs:
---
""" + logs[:8000] + """
---

If the fix is to set environment variables on the Deployment, reply with ONLY this JSON (no markdown):
{"env_vars":[{"name":"VAR_NAME","value":"value"},...]}

If you cannot suggest env vars, reply: {"env_vars":[]}
"""
    try:
        resp = openai_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        text = (resp.choices[0].message.content or "").strip()
        obj = parse_json_object(text)
        raw = obj.get("env_vars")
        if not isinstance(raw, list):
            return None
        out: List[Dict[str, str]] = []
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                out.append(
                    {
                        "name": str(item["name"]),
                        "value": str(item.get("value", "")),
                    }
                )
        return out
    except Exception as e:
        logger.warning("LLM env suggestion failed: %s", e)
        return None


class ToolCaller(Protocol):
    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any: ...


@dataclass
class RemediationOptions:
    """Options mirroring client-gpt remediate CLI flags."""

    approve: bool = False
    dry_run: bool = False
    include_openshift_namespaces: bool = False
    #: If True (default), never auto-select pods in default, kube-*, or openshift-*.
    app_namespaces_only: bool = True
    remediate_namespace: Optional[str] = None
    remediate_pod: Optional[str] = None
    remediate_use_llm: bool = False
    model: str = "granite-8b"


@dataclass
class RemediationResult:
    success: bool
    summary: str
    applied_patch: bool = False


async def _default_emit(msg: str) -> None:
    print(msg, flush=True)


EmitFn = Union[Callable[[str], None], Callable[[str], Awaitable[None]]]


async def _emit(emit: EmitFn, msg: str) -> None:
    r = emit(msg)
    if inspect.isawaitable(r):
        await r


async def run_crashloop_remediation_async(
    tool_caller: ToolCaller,
    *,
    options: RemediationOptions,
    openai_client: OpenAI,
    emit: EmitFn = _default_emit,
) -> RemediationResult:
    """
    Run the CrashLoop remediation workflow using the given MCP-style tool caller.
    """
    app_only = options.app_namespaces_only and options.remediate_namespace is None
    logger.info(
        "Remediation start: app_namespaces_only=%s include_openshift=%s ns=%s pod=%s dry_run=%s",
        app_only,
        options.include_openshift_namespaces,
        options.remediate_namespace,
        options.remediate_pod,
        options.dry_run,
    )
    await _emit(emit, "\n=== Workflow: CrashLoop remediation (MCP + logic) ===\n")

    try:
        res = await tool_caller.call_tool(
            TOOL_LIST_PODS_ERROR,
            {"include_openshift_namespaces": options.include_openshift_namespaces},
        )
        raw = extract_text(res)
    except Exception as e:
        logger.exception("listar_pods_em_erro_cluster failed: %s", e)
        await _emit(emit, f"Failed to list problem pods: {e}")
        return RemediationResult(False, f"list pods failed: {e}")

    logger.info("Problem pod listing length=%d chars", len(raw or ""))
    await _emit(emit, "Raw listing (filtered lines may apply):\n")
    await _emit(emit, raw)
    await _emit(emit, "")

    # Full CrashLoop set for diagnostics (no app-only filter)
    all_crashloop = parse_problem_pod_lines(
        raw,
        crashloop_only=True,
        include_openshift_namespaces=True,
        namespace_filter=options.remediate_namespace,
        pod_filter=options.remediate_pod,
        app_namespaces_only=False,
    )

    targets = parse_problem_pod_lines(
        raw,
        crashloop_only=True,
        include_openshift_namespaces=options.include_openshift_namespaces,
        namespace_filter=options.remediate_namespace,
        pod_filter=options.remediate_pod,
        app_namespaces_only=app_only,
    )

    logger.info(
        "Parsed CrashLoop pods: total_in_listing=%d in_remediation_scope=%d",
        len(all_crashloop),
        len(targets),
    )

    if not targets:
        if not all_crashloop:
            msg = "No CrashLoop pods found in the MCP listing. Check kube access / cluster state."
            await _emit(emit, msg)
            logger.warning(msg)
            return RemediationResult(False, msg)
        openshift_only = all(ns.startswith("openshift-") for ns, _ in all_crashloop)
        if openshift_only and not options.include_openshift_namespaces:
            msg = (
                "CrashLoop pod(s) are only in openshift-* namespaces right now. "
                "To remediate infra pods, use include_openshift_namespaces=True."
            )
            await _emit(emit, msg)
            await _emit(emit, f"Detected (openshift only): {all_crashloop}")
            logger.warning("%s | %s", msg, all_crashloop)
            return RemediationResult(False, msg)
        only_infra = all(
            _should_skip_namespace_for_app_only(
                ns, include_openshift_namespaces=options.include_openshift_namespaces
            )
            for ns, _ in all_crashloop
        )
        if only_infra and app_only:
            msg = (
                "All CrashLoop pods are in platform namespaces (default, kube-*, openshift-*). "
                "Remediation targets application namespaces only by default. "
                "Use include_openshift_namespaces, set allow_system_namespaces / --allow-system-namespaces, "
                "or pin namespace with remediate_namespace."
            )
            await _emit(emit, msg)
            await _emit(emit, f"Skipped by policy: {all_crashloop}")
            logger.warning("%s | skipped=%s", msg, all_crashloop)
            return RemediationResult(False, msg)
        msg = "No CrashLoop pods in scope after filters. Adjust namespace/pod filters."
        await _emit(emit, msg)
        logger.warning("%s | all_crashloop=%s", msg, all_crashloop)
        return RemediationResult(False, msg)

    ns, pod = targets[0]
    if len(targets) > 1:
        await _emit(emit, f"Multiple targets ({len(targets)}); remediating first: {ns}/{pod}")
        await _emit(emit, "Others: " + ", ".join(f"{n}/{p}" for n, p in targets[1:5]))
        if len(targets) > 5:
            await _emit(emit, "...")
    else:
        await _emit(emit, f"Selected pod: {ns}/{pod}")

    deployment = infer_deployment_from_pod_name(pod)
    logger.info("Target pod=%s/%s deployment=%s", ns, pod, deployment)
    await _emit(emit, f"Inferred Deployment name: {deployment} (from pod name)")

    try:
        log_res = await tool_caller.call_tool(
            TOOL_VER_LOGS,
            {"pod": pod, "namespace": ns, "tail_lines": 120},
        )
        logs = extract_text(log_res)
    except Exception as e:
        logger.exception("ver_logs_pod failed ns=%s pod=%s: %s", ns, pod, e)
        await _emit(emit, f"Failed to read logs: {e}")
        return RemediationResult(False, f"logs failed: {e}")

    await _emit(emit, "\n--- Pod logs (tail) ---\n")
    await _emit(emit, logs)
    await _emit(emit, "\n--- End logs ---\n")

    env_fixes = extract_env_fixes_from_logs(logs)
    if not env_fixes and options.remediate_use_llm:
        await _emit(emit, "No regex match; trying LLM for env suggestions...")
        env_fixes = suggest_env_fixes_with_llm(
            openai_client=openai_client,
            model=options.model,
            logs=logs,
        ) or []

    if not env_fixes:
        msg = (
            "Could not derive env vars from logs (extend extract_env_fixes_from_logs "
            "or enable remediate_use_llm with a working LLM endpoint)."
        )
        await _emit(emit, msg)
        logger.warning("No env_fixes derived for %s/%s", ns, pod)
        return RemediationResult(False, msg)

    await _emit(emit, "Planned env patch:")
    await _emit(emit, json.dumps(env_fixes, indent=2))
    logger.info("Env patch planned for %s/%s deployment=%s keys=%s", ns, pod, deployment, [e["name"] for e in env_fixes])

    if options.dry_run:
        await _emit(emit, "\n[dry-run] not calling definir_env_deployment.")
        logger.info("Dry run complete; no cluster write")
        return RemediationResult(True, "Dry run: plan only, no cluster write.", applied_patch=False)

    if not options.approve:
        msg = "Remediation requires approval (approve=True) to call definir_env_deployment."
        await _emit(emit, f"\n{msg}")
        logger.warning("Stopped: approval required")
        return RemediationResult(False, msg)

    try:
        logger.info("Calling definir_env_deployment deployment=%s ns=%s", deployment, ns)
        fix_res = await tool_caller.call_tool(
            TOOL_SET_ENV,
            {
                "deployment": deployment,
                "namespace": ns,
                "env_vars": env_fixes,
            },
        )
        await _emit(emit, "\nRemediation result:")
        out = extract_text(fix_res)
        await _emit(emit, out)
        logger.info("definir_env_deployment ok deployment=%s ns=%s", deployment, ns)
        return RemediationResult(True, out or "Env patch applied.", applied_patch=True)
    except Exception as e:
        logger.exception("definir_env_deployment failed: %s", e)
        await _emit(emit, f"definir_env_deployment failed: {e}")
        return RemediationResult(False, f"patch failed: {e}")


class FastMcpToolCaller:
    """Adapter: fastmcp Client -> ToolCaller (stdio subprocess or streamable HTTP to server-gpt)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        return await self._client.call_tool(name, arguments)
