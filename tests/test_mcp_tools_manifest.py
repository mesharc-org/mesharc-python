"""scripts/mcp_tools.py: the tool list the website, mesharc-mcp's README and
the Claude Code plugin are generated from, written by the release workflow."""
import json
import pathlib
import subprocess
import sys

import pytest

pytest.importorskip("mcp")

from mesharc import __version__
from mesharc import mcp as mcp_mod

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "mcp_tools.py"


def _manifest():
    out = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, check=True, encoding="utf-8")
    return json.loads(out.stdout)


def test_the_manifest_names_every_registered_tool_once_in_order():
    m = _manifest()
    registered = [t.name for t in mcp_mod._build()._tool_manager.list_tools()]
    assert [t["name"] for t in m["tools"]] == registered
    assert m["count"] == len(registered) == len(set(registered))
    assert m["version"] == __version__


def test_each_tool_has_a_title_and_a_one_line_summary():
    for t in _manifest()["tools"]:
        assert t["title"], t["name"]
        assert t["summary"] and "\n" not in t["summary"], t["name"]


def test_the_flags_match_the_tools_own_annotations():
    m = {t["name"]: t for t in _manifest()["tools"]}
    for t in mcp_mod._build()._tool_manager.list_tools():
        a = t.annotations
        assert m[t.name]["readOnly"] is bool(a and a.read_only_hint), t.name
        assert m[t.name]["destructive"] is bool(a and a.destructive_hint), t.name
