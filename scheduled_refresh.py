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
from datetime import datetime

from data_loader import refresh_if_changed


def main() -> None:
    result = asyncio.run(refresh_if_changed())
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] refresh_if_changed -> {json.dumps(result)}", flush=True)


if __name__ == "__main__":
    main()
