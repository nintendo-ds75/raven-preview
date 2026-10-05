"""Run Raven's stdio MCP transport against a local database."""

import sys

from bridge.__main__ import main

if __name__ == "__main__":
    sys.argv.insert(1, "mcp")
    main()
