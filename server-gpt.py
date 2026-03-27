"""
FastMCP server — registers tools implemented in openshift_tool_handlers.

- MCP_TRANSPORT=stdio (default): for local `client-gpt.py server-gpt.py`
- MCP_TRANSPORT=streamable-http: bind MCP_HTTP_HOST:MCP_HTTP_PORT, endpoint …/mcp (OpenShift Service)
"""

from __future__ import annotations

from pathlib import Path
import os
from typing import Dict, List, Optional

from mcp.server.fastmcp import FastMCP

import openshift_tool_handlers as h


def _transport() -> str:
    t = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    if t in ("http", "streamable-http", "streamable_http"):
        return "streamable-http"
    if t == "sse":
        return "sse"
    return "stdio"


def _bind() -> tuple[str, int]:
    if _transport() == "stdio":
        return ("127.0.0.1", 8000)
    host = os.environ.get("MCP_HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_HTTP_PORT", "9000"))
    return (host, port)


_h, _p = _bind()
mcp = FastMCP("DemoOpenShift", host=_h, port=_p)


@mcp.tool()
def verificar_status_sistema(componente: str) -> str:
    """Checks basic status for a component. Useful before updates."""
    return h.verificar_status_sistema(componente)


@mcp.tool()
def listar_nodes() -> str:
    """Lists cluster nodes with basic info (name, ready, kubelet version)."""
    return h.listar_nodes()


@mcp.tool()
def listar_pods(namespace: str) -> str:
    """Lists pods in a given namespace with basic info."""
    return h.listar_pods(namespace)


@mcp.tool()
def listar_pods_em_erro_cluster(include_openshift_namespaces: bool = False) -> str:
    """Lists pods in problematic states. By default skips openshift-*, kube-*, and default (app focus)."""
    return h.listar_pods_em_erro_cluster(include_openshift_namespaces)


@mcp.tool()
def iniciar_upgrade_openshift(version: str, image: Optional[str] = None) -> str:
    """Starts an OpenShift cluster upgrade via ClusterVersion."""
    return h.iniciar_upgrade_openshift(version, image)


@mcp.tool()
def ver_logs_pod(
    pod: str,
    namespace: str,
    container: Optional[str] = None,
    tail_lines: Optional[int] = 100,
    timestamps: bool = False,
) -> str:
    """Retrieves the logs of a pod in the cluster."""
    return h.ver_logs_pod(pod, namespace, container, tail_lines, timestamps)


@mcp.tool()
def definir_env_deployment(
    deployment: str,
    namespace: str,
    env_vars: List[Dict[str, str]],
) -> str:
    """Sets environment variables on a Deployment (merged with existing)."""
    return h.definir_env_deployment(deployment, namespace, env_vars)


@mcp.resource("docs://mcpreadme")
def obter_mcpreadme() -> str:
    """Returns the full docs/mcpreadme.md content for MCP clients."""
    MCPREADME_PATH = Path(__file__).parent / "docs" / "mcpreadme.md"
    return MCPREADME_PATH.read_text(encoding="utf-8")


if __name__ == "__main__":
    mcp.run(transport=_transport())  # type: ignore[arg-type]
