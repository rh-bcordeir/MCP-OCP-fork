"""
Runs remediation_workflow inside the API process.

- Default: in-process dispatch (openshift_tool_handlers.MCP_TOOL_DISPATCH).
- REMEDIATION_MCP_URL set: FastMCP Client over streamable HTTP to server-gpt.py (e.g. mcp-server Service).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable, Optional

from openai import OpenAI

logger = logging.getLogger(__name__)

from .inprocess_mcp import InProcessOpenShiftToolCaller
from .repo_path import ensure_basic_mcp_on_path


def _build_openai_client() -> OpenAI:
    api_base = (
        os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("GRANITE_API_BASE")
        or ""
    ).rstrip("/")
    api_key = (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("GRANITE_API_TOKEN")
        or ""
    )
    if api_base:
        return OpenAI(api_key=api_key, base_url=api_base)
    return OpenAI(api_key=api_key)


def _mcp_streamable_http_headers() -> Optional[dict[str, str]]:
    raw = (os.environ.get("REMEDIATION_MCP_HTTP_HEADERS_JSON") or "").strip()
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(
                "REMEDIATION_MCP_HTTP_HEADERS_JSON must be valid JSON object"
            ) from e
    bearer = (os.environ.get("REMEDIATION_MCP_BEARER_TOKEN") or "").strip()
    if bearer:
        return {"Authorization": f"Bearer {bearer}"}
    return None


async def execute_remediation_in_process(
    *,
    approve: bool,
    dry_run: bool = False,
    include_openshift_namespaces: bool = False,
    allow_system_namespaces: bool = False,
    remediate_namespace: Optional[str] = None,
    remediate_pod: Optional[str] = None,
    remediate_use_llm: bool = False,
    model: Optional[str] = None,
    emit: Callable[[str], None] = print,
):
    """
    Run CrashLoop remediation: either in-process tool dispatch or remote MCP (streamable HTTP).

    Set REMEDIATION_MCP_URL to the MCP streamable HTTP endpoint (e.g.
    http://mcp-server.<namespace>.svc.cluster.local:9000/mcp) to use server-gpt.py in-cluster.
    """
    ensure_basic_mcp_on_path()
    from remediation_workflow import (  # noqa: WPS433
        FastMcpToolCaller,
        RemediationOptions,
        run_crashloop_remediation_async,
    )

    mcp_url = (os.environ.get("REMEDIATION_MCP_URL") or "").strip()

    logger.info(
        "execute_remediation approve=%s dry_run=%s mcp_url=%s",
        approve,
        dry_run,
        "set" if mcp_url else "(in-process)",
    )

    opts = RemediationOptions(
        approve=approve,
        dry_run=dry_run,
        include_openshift_namespaces=include_openshift_namespaces,
        app_namespaces_only=not allow_system_namespaces,
        remediate_namespace=remediate_namespace,
        remediate_pod=remediate_pod,
        remediate_use_llm=remediate_use_llm,
        model=model or os.environ.get("LLM_MODEL", "granite-8b"),
    )

    oai = _build_openai_client()

    if mcp_url:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        headers = _mcp_streamable_http_headers()
        transport = StreamableHttpTransport(url=mcp_url, headers=headers)
        async with Client(transport) as client:
            try:
                await client.ping()
            except Exception as e:
                logger.warning("MCP ping failed (continuing): %s", e)
            caller = FastMcpToolCaller(client)
            return await run_crashloop_remediation_async(
                caller,
                options=opts,
                openai_client=oai,
                emit=emit,
            )

    caller = InProcessOpenShiftToolCaller()
    return await run_crashloop_remediation_async(
        caller,
        options=opts,
        openai_client=oai,
        emit=emit,
    )
