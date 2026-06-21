"""Guards for two safety-critical MCP conventions, so they can't silently regress:
   (1) diagnostics never touch stdout (the stdio JSON channel), and
   (2) tools tolerate an empty/not-yet-built snapshot without throwing.
"""
import asyncio
import io
import sys

import pytest

import server


def test_log_writes_to_stderr_not_stdout():
    """data_loader.log() must stay off stdout — any stray stdout corrupts MCP JSON."""
    import data_loader
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        data_loader.log("stderr-channel-check")
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    assert out.getvalue() == ""                        # stdio channel stayed clean
    assert "stderr-channel-check" in err.getvalue()    # message went to stderr


# Read-only tools called before build_snapshot() — the in-memory snapshot is empty.
# The critical invariant: they return a value (dict or list), never raise.
EMPTY_SNAPSHOT_CALLS = [
    ("search_institutions", {"query": "test"}),
    ("get_institution_profile", {"identifier": "628"}),
    ("reconcile_institution", {"name": "Test Bank"}),
    ("crosswalk_identifiers", {"identifier": "628", "id_type": "cert"}),
    ("get_top_institutions", {}),
    ("list_institutions", {}),
    ("get_institution_history", {"rssd_id": "852218"}),
]


@pytest.mark.parametrize("tool_name,kwargs", EMPTY_SNAPSHOT_CALLS)
def test_tools_do_not_throw_on_empty_snapshot(tool_name, kwargs):
    result = asyncio.run(getattr(server, tool_name)(**kwargs))
    assert isinstance(result, (dict, list))
