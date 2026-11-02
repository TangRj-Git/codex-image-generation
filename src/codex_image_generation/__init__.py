"""Codex image-generation MCP server."""

__version__ = "0.4.0"


def main() -> None:
    """Entry point for the installed console script."""
    from .cli import main as run

    run()
