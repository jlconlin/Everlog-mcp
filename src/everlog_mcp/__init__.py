"""Read-only MCP server for the Everlog journal app."""


def main() -> None:
    from .server import server
    server.run()  # stdio; all logging goes to stderr
