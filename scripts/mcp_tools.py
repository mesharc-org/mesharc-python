"""Print the MCP server's tools as JSON: the list every place that names them
is generated from (the website, mesharc-mcp's README, the Claude Code plugin).

    python scripts/mcp_tools.py > mcp-tools.json

It reads the installed package (run as a script, the repository's own folder
is not on the path), so in the release workflow it describes the wheel that
was just built. Each tool, in the order the server registers them:

    {"name", "title", "summary", "readOnly", "destructive"}

`summary` is the first line of the tool's description: what it is for.
"""

import json
import sys


def tools():
    from mesharc import __version__
    from mesharc.mcp import _build

    out = []
    for t in _build()._tool_manager.list_tools():
        a = t.annotations
        out.append({
            "name": t.name,
            "title": t.title or "",
            "summary": (t.description or "").strip().splitlines()[0] if t.description else "",
            "readOnly": bool(a and a.read_only_hint),
            "destructive": bool(a and a.destructive_hint),
        })
    return {"version": __version__, "count": len(out), "tools": out}


def main():
    json.dump(tools(), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
