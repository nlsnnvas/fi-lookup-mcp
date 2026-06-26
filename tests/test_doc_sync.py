"""Hermetic guard: the hardcoded figures in README.md / CLAUDE.md must agree with
docs_stats.json (the source of truth that tools/sync_docs.py maintains). This catches a
hand-edit to one without the other. No snapshot/network needed — pure file comparison —
so it runs in CI. The live-vs-docs drift check (`sync_docs.py --check`) is the separate,
snapshot-dependent step run after a data refresh.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import sync_docs  # noqa: E402  (path set above)

STATS = json.load(open(os.path.join(ROOT, "docs_stats.json")))
README = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
CLAUDEMD = open(os.path.join(ROOT, "CLAUDE.md"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()


def test_count_figures_present_in_readme():
    """Every count in docs_stats.json must appear (comma-formatted) in README.md."""
    for field in ("active_total", "banks", "credit_unions", "historical"):
        token = sync_docs._fmt_int(STATS[field])
        assert token in README, f"{field}={token} missing from README.md (docs/json out of sync)"


def test_gold_marker_spans_match_stats():
    """The SYNC-marker spans must equal what the template renders from docs_stats.json."""
    import re

    cases = [("gold_readme", sync_docs._gold_readme, README),
             ("gold_claude", sync_docs._gold_claude, CLAUDEMD)]
    for key, tmpl, text in cases:
        m = re.search(rf"<!--SYNC:{key}-->((?:(?!<!--).)*?)<!--/SYNC:{key}-->", text, re.S)
        assert m, f"marker SYNC:{key} not found — sync_docs.py can't keep it current"
        assert m.group(1) == tmpl(STATS["gold"]), (
            f"SYNC:{key} span is stale vs docs_stats.json — run `python tools/sync_docs.py`")


def test_documented_tool_count_matches_server():
    """The '<N> tools' figure in the docs must equal the actual @mcp.tool count in
    server.py. Code-driven (not data-driven), so it's guarded here at commit time rather
    than auto-written on a data refresh: adding/removing a tool without updating the docs
    fails this hermetic test in the same PR."""
    import re

    actual = len(re.findall(r"@mcp\.tool", SERVER))
    for name, text in (("README.md", README), ("CLAUDE.md", CLAUDEMD)):
        documented = [int(n) for n in re.findall(r"(\d+)\s+(?:MCP\s+)?tools\b", text)]
        assert documented, f"no '<N> tools' phrase found in {name}"
        for n in documented:
            assert n == actual, f"{name} says '{n} tools' but server.py defines {actual}"
