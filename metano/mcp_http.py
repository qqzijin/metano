"""Remote read-only MCP server (Streamable HTTP) for metano.

Exposes a deliberately small, read-only subset of the tools defined in
:mod:`metano.mcp_server` over the Streamable HTTP transport, so an external MCP
host (e.g. a remote Claude) can query metano's data without any write or
destructive capability.

The tool functions are reused directly from ``metano.mcp_server`` (zero
duplicate logic); only the whitelist in :mod:`metano.mcp_policy` decides what
is registered. Authentication/authorization is NOT handled here — it is the
responsibility of the layer that mounts this Starlette app.
"""
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import mcp_server
from . import mcp_policy

# FastMCP's default transport security only accepts localhost Host headers.
# To let other machines on the LAN reach this MCP server, list their host/ip
# patterns in the METANO_ALLOWED_HOSTS env var (comma-separated, each with a
# port wildcard, e.g. "192.168.1.50:*,nas.local:*"). Only loopback is allowed
# by default so the repo carries no private network topology.
_ALLOWED_HOSTS = ["localhost:*", "127.0.0.1:*", "[::1]:*"] + [
    h.strip() for h in os.environ.get("METANO_ALLOWED_HOSTS", "").split(",") if h.strip()
]


def create_http_app():
    """Build a Starlette app serving the read-only MCP tools over Streamable HTTP.

    Returns
    -------
    starlette.applications.Starlette
        The HTTP app to be mounted by the web server / gateway layer.
    """
    mcp = FastMCP(
        "metano-remote",
        stateless_http=True,
        transport_security=TransportSecuritySettings(allowed_hosts=_ALLOWED_HOSTS),
    )
    # Register only the read-only whitelist, reusing mcp_server's function
    # objects (FastMCP introspects the function signature to build the schema).
    for name in mcp_policy.READ_TOOLS:
        fn = getattr(mcp_server, name, None)
        if fn is not None:
            mcp.tool()(fn)
    return mcp.streamable_http_app()
