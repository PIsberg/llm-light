"""A stdio MCP server for the MCP tests: `nonce` returns a value only this process knows.

A reply that quotes the nonce proves the tool ran, since the model cannot guess it.
`fail` returns an error result, `big` a result longer than any tool output cap, and
`picture` an image, for the result conversion. Run with: python mcp_nonce_server.py <nonce>
"""

import sys

from mcp.server.mcpserver import MCPServer
from mcp_types import ImageContent

NONCE = sys.argv[1] if len(sys.argv) > 1 else "no-nonce"

server = MCPServer("nonce")


@server.tool()
def nonce(label: str = "") -> str:
    """Return this server's secret nonce, prefixed with the label."""
    return f"{label}{NONCE}"


@server.tool()
def fail() -> str:
    """Always fails."""
    raise ValueError("fail was called")


@server.tool()
def big() -> str:
    """Return 100,000 characters."""
    return "x" * 100_000


@server.tool()
def picture() -> ImageContent:
    """Return a one-pixel image."""
    return ImageContent(type="image", data="iVBORw0KGgo=", mime_type="image/png")


if __name__ == "__main__":
    server.run("stdio")
