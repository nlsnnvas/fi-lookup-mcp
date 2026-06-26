#!/usr/bin/env python
"""
scheduled_refresh.py
Monthly conditional-refresh runner for cron/launchd.

Safe to run as often as you like: it rebuilds the snapshot ONLY when an upstream
source actually changed (FFIEC ZIP content, or a newly published FDIC/NCUA
quarter). When nothing changed it does cheap fingerprint checks and exits, so a
no-op run costs almost nothing.

Not the MCP server — printing to stdout here is fine; cron/launchd capture it.
"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime

from data_loader import refresh_if_changed


def main() -> None:
    result = asyncio.run(refresh_if_changed())
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] refresh_if_changed -> {json.dumps(result)}", flush=True)

    # Trend the non-deterministic fields every run: append one metrics record to
    # cache/accuracy_history.jsonl and print the delta + any threshold alerts. Isolated
    # so a monitoring hiccup never fails the refresh itself.
    try:
        from metrics_snapshot import emit
        _, report = emit()
        print(report, flush=True)
    except Exception as e:  # noqa: BLE001 — monitoring must not break the refresh
        print(f"[{stamp}] metrics_snapshot skipped: {type(e).__name__}: {e}", flush=True)

    # When the data actually advanced, refresh the hardcoded figures in README.md /
    # CLAUDE.md so the docs never quote a stale quarter. No-op runs skip this (nothing
    # moved). Isolated so a doc-sync hiccup never fails the refresh itself.
    if result.get("changed"):
        try:
            out = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__), "tools", "sync_docs.py")],
                capture_output=True, text=True,
            )
            print(out.stdout.strip() or f"[{stamp}] sync_docs: (no output)", flush=True)
            if out.returncode != 0:
                print(f"[{stamp}] sync_docs stderr: {out.stderr.strip()}", flush=True)
        except Exception as e:  # noqa: BLE001 — doc sync must not break the refresh
            print(f"[{stamp}] sync_docs skipped: {type(e).__name__}: {e}", flush=True)


if __name__ == "__main__":
    main()
