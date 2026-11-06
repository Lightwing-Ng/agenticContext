"""USB iPhone control through WebDriverAgent, vendored from iPhone Use 0.3.10.

The modules in this package import ``fcntl`` and drive macOS-only tooling, so this
package initializer deliberately imports none of them. Callers import the runtime
lazily, and only on macOS. ``python3 -m app.core.iphone.mcp_server`` runs the stdio
MCP server from the repository root.
"""

# Code version: v1.0.0-claude.0
