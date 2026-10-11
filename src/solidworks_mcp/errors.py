"""Error type surfaced to the MCP client as a tool error (isError=true)."""

from mcp.server.mcpserver.exceptions import ToolError


class SwError(ToolError):
    """A user-facing failure: bad arguments, missing entities, or an operation SolidWorks rejected."""
