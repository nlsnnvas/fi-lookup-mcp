"""
server.py
FI-Lookup MCP Server — financial institution lookup and reconciliation.
Public FDIC/NCUA data only. Not connected to any employer systems.
"""

from contextlib import asynccontextmanager
from fastmcp import FastMCP
from data_loader import build_snapshot, get_all_institutions, get_nic_names
from rapidfuzz import fuzz


@asynccontextmanager
async def lifespan(app):
    # build_snapshot reads from cache if available — no network calls on warm start
    await build_snapshot()
    yield


mcp = FastMCP(
    name="fi-lookup",
    lifespan=lifespan,
    instructions=(
        "Look up and reconcile US financial institutions using public FDIC and NCUA data. "
        "Tools: search_institutions (name search), get_institution_profile (by ID), "
        "reconcile_institution (best-match scoring), crosswalk_identifiers (ID translation), "
        "get_institution_history (merger/acquisition/rebrand lineage), "
        "get_recent_changes (change feed for dataset maintenance), "
        "list_institutions (browse the full dataset with all fields — search/filter/sort/export)."
    )
)


# ---------------------------------------------------------------------------
# Tool 1: search_institutions
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_institutions(
    query: str,
    institution_type: str = "all",
    state: str = "",
    limit: int = 10,
) -> list[dict]:
    """
    Search for US financial institutions by name across FDIC banks and NCUA credit unions.

    Args:
        query: Institution name to search for (e.g. "Mountain America", "Chase", "Navy Federal")
        institution_type: Filter by type — "bank", "cu" (credit union), or "all" (default)
        state: Optional 2-letter state abbreviation to narrow results (e.g. "UT", "CA")
        limit: Max results to return (default 10, max 50)

    Returns:
        List of matching institutions with identifiers and confidence scores.
    """
    limit = min(limit, 50)
    query_clean = query.strip().lower()

    if not query_clean:
        return [{"error": "query cannot be empty"}]

    institutions = get_all_institutions()
    if not institutions:
        return [{"error": "Data snapshot not loaded. Server may still be starting up."}]

    if institution_type == "bank":
        pool = [i for i in institutions if i["source"] == "fdic"]
    elif institution_type == "cu":
        pool = [i for i in institutions if i["source"] == "ncua"]
    else:
        pool = institutions

    if state:
        state_upper = state.upper()
        state_full_map = {
            "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
            "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
            "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
            "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
            "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
            "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
            "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
            "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
            "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
            "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
            "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
            "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
            "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
        }
        state_full = state_full_map.get(state_upper, "")
        pool = [
            i for i in pool
            if i.get("state", "").upper() == state_upper
            or i.get("state", "") == state_full
        ]

    scored = []
    for inst in pool:
        name = inst.get("name", "")
        score = fuzz.token_set_ratio(query_clean, name.lower()) / 100.0
        if score >= 0.30:
            scored.append((score, inst))

    scored.sort(key=lambda x: x[0], reverse=True)

    results = []
    for score, inst in scored[:limit]:
        result = {
            "name": inst.get("name", ""),
            "source": inst["source"],
            "type": "Credit Union" if inst["source"] == "ncua" else "Bank",
            "match_score": round(score, 3),
            "city": inst.get("city", ""),
            "state": inst.get("state", ""),
        }
        if inst["source"] == "fdic":
            result["fdic_cert"] = inst.get("cert", "")
            result["rssdid"] = inst.get("rssdid", "")
        else:
            result["ncua_charter"] = inst.get("charter_number", "")
            result["rssdid"] = inst.get("rssdid", "")
        results.append(result)

    return results if results else [{"message": f"No institutions matched '{query}'"}]


# ---------------------------------------------------------------------------
# Tool 2: get_institution_profile
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_institution_profile(
    identifier: str,
    id_type: str = "auto",
) -> dict:
    """
    Get the full regulatory profile for a financial institution by identifier.

    Args:
        identifier: The institution's ID — FDIC cert number, NCUA charter number, or RSSD ID
        id_type: Type of identifier — "fdic_cert", "ncua_charter", "rssdid", or "auto" (default)

    Returns:
        Full institution profile including name, location, identifiers, type, and regulator.
    """
    identifier = identifier.strip()
    institutions = get_all_institutions()

    if not institutions:
        return {"error": "Data snapshot not loaded."}

    def match(inst: dict) -> bool:
        if id_type in ("fdic_cert", "auto"):
            if inst.get("cert") == identifier and inst["source"] == "fdic":
                return True
        if id_type in ("ncua_charter", "auto"):
            if inst.get("charter_number") == identifier and inst["source"] == "ncua":
                return True
        if id_type in ("rssdid", "auto"):
            if inst.get("rssdid") == identifier and identifier not in ("", "0"):
                return True
        return False

    candidates = [i for i in institutions if match(i)]

    if not candidates:
        return {"error": f"No institution found with {id_type} = '{identifier}'"}

    results = []
    for inst in candidates:
        is_cu = inst["source"] == "ncua"
        profile = {
            "name": inst.get("name", ""),
            "source": inst["source"],
            "regulator": "NCUA" if is_cu else "FDIC / OCC / Federal Reserve",
            "type": "Credit Union" if is_cu else "Bank / Thrift",
            "city": inst.get("city", ""),
            "state": inst.get("state", ""),
            "fdic_cert": inst.get("cert", "") if not is_cu else "N/A — NCUA-regulated institution",
            "ncua_charter": inst.get("charter_number", "") if is_cu else "N/A — FDIC-regulated institution",
            "rssdid": inst.get("rssdid", "") or "Not available",
            "aba_routing": inst.get("aba_routing", "") or "Not available",
            "deposit_accounts": inst.get("deposit_accounts", "") or "Not available",
            "web_address": inst.get("web_address", "") or "Not available",
            "charter_type": {
                "1": "Federally Chartered Credit Union (FCU)",
                "2": "State Chartered, Federally Insured (FISCU)",
                "3": "State Chartered, Privately Insured",
            }.get(inst.get("charter_type", ""), None) if is_cu else None,
            "inst_category": inst.get("inst_category", "") or None,
        }
        excluded = {"source", "cert", "charter_number", "total_assets", "aba_routing",
                    "rssdid", "name", "city", "state", "deposit_accounts",
                    "web_address", "charter_type", "inst_category"}
        for k, v in inst.items():
            if k not in excluded and v not in ("", None, "0", 0):
                profile[k] = v
        profile = {k: v for k, v in profile.items() if v is not None and v != ""}
        results.append(profile)

    return results[0] if len(results) == 1 else {"matches": results}


# ---------------------------------------------------------------------------
# Tool 3: reconcile_institution
# ---------------------------------------------------------------------------

@mcp.tool()
async def reconcile_institution(
    name: str,
    city: str = "",
    state: str = "",
    fdic_cert: str = "",
    ncua_charter: str = "",
    rssd_id: str = "",
    top_n: int = 5,
) -> list[dict]:
    """
    Reconcile a messy external institution record against canonical FDIC/NCUA data.
    Returns ranked candidates with confidence scores (0-1) and human-readable match reasons.

    Args:
        name: Institution name from the external record (can be dirty/abbreviated)
        city: City from the external record (optional but improves scoring)
        state: State from the external record — 2-letter abbrev preferred (optional)
        fdic_cert: FDIC certificate number if known (triggers exact-match override)
        ncua_charter: NCUA charter number if known (triggers exact-match override)
        rssd_id: RSSD ID if known (triggers exact-match override)
        top_n: Number of candidates to return (default 5)

    Returns:
        Ranked list of candidate matches, each with confidence score and match reasons.
    """
    from reconciler import reconcile

    institutions = get_all_institutions()
    if not institutions:
        return [{"error": "Data snapshot not loaded."}]

    if not name.strip():
        return [{"error": "name is required"}]

    return reconcile(
        query_name=name,
        query_city=city,
        query_state=state,
        query_cert=fdic_cert,
        query_charter=ncua_charter,
        query_rssd=rssd_id,
        institutions=institutions,
        top_n=top_n,
    )


# ---------------------------------------------------------------------------
# Tool 4: crosswalk_identifiers
# ---------------------------------------------------------------------------

@mcp.tool()
async def crosswalk_identifiers(
    identifier: str,
    id_type: str,
) -> dict:
    """
    Translate between FDIC cert, NCUA charter, and RSSD ID for a financial institution.

    Args:
        identifier: The known identifier value
        id_type: Type of the input identifier — "fdic_cert", "ncua_charter", or "rssdid"

    Returns:
        All known identifiers for the matched institution, with explanation of any gaps.
    """
    institutions = get_all_institutions()
    if not institutions:
        return {"error": "Data snapshot not loaded."}

    identifier = identifier.strip()

    matches = []
    for inst in institutions:
        if id_type == "fdic_cert" and inst.get("cert") == identifier and inst["source"] == "fdic":
            matches.append(inst)
        elif id_type == "ncua_charter" and inst.get("charter_number") == identifier and inst["source"] == "ncua":
            matches.append(inst)
        elif id_type == "rssdid" and inst.get("rssdid") == identifier and identifier not in ("", "0"):
            matches.append(inst)

    if not matches:
        return {"error": f"No institution found with {id_type} = '{identifier}'"}

    results = []
    for inst in matches:
        is_cu = inst["source"] == "ncua"
        entry = {
            "name": inst.get("name", ""),
            "type": "Credit Union" if is_cu else "Bank / Thrift",
            "regulator": "NCUA" if is_cu else "FDIC / OCC / Federal Reserve",
            "identifiers": {
                "fdic_cert": inst.get("cert") if not is_cu else None,
                "ncua_charter": inst.get("charter_number") if is_cu else None,
                "rssdid": inst.get("rssdid") or None,
                "aba_routing": inst.get("aba_routing") or None,
            },
            "crosswalk_notes": [],
        }

        if is_cu:
            entry["crosswalk_notes"].append(
                "Credit unions are NCUA-regulated and do not have FDIC certificate numbers. "
                "FDIC cert is not applicable."
            )
        else:
            entry["crosswalk_notes"].append(
                "Banks are FDIC-regulated and do not have NCUA charter numbers. "
                "NCUA charter is not applicable."
            )

        if not inst.get("rssdid") or inst.get("rssdid") == "0":
            entry["crosswalk_notes"].append(
                "RSSD ID not available in this dataset for this institution."
            )

        results.append(entry)

    return results[0] if len(results) == 1 else {"matches": results}


# ---------------------------------------------------------------------------
# Tool 5: refresh_cache
# ---------------------------------------------------------------------------

@mcp.tool()
async def refresh_cache() -> dict:
    """
    Rebuild the local data snapshot from scratch.
    Re-fetches FDIC data from the BankFind API and re-reads NCUA and FFIEC data
    from the ZIP files in cache/. Use this when you want to pull fresh data
    without restarting the server.

    Returns:
        Summary of what was refreshed and how many records were loaded per source.
    """
    from data_loader import CACHE_DIR, get_data_as_of

    warnings = []

    # FDIC is fetched live (latest quarter auto-discovered); NCUA is auto-downloaded
    # (latest quarter); only FFIEC must be supplied manually.
    ffiec_zips = list(CACHE_DIR.glob("CSV_ATTRIBUTES_ACTIVE*.zip"))
    if not ffiec_zips:
        warnings.append(
            "No FFIEC attributes ZIP found in cache/. ABA routing numbers will be unavailable. "
            "Download CSV_ATTRIBUTES_ACTIVE*.zip from ffiec.gov and place it in cache/."
        )

    try:
        institutions = await build_snapshot(force_refresh=True)
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "hint": "Check network access for FDIC/NCUA and that your FFIEC ZIPs in cache/ are not corrupted.",
        }

    fdic_count = sum(1 for i in institutions if i["source"] == "fdic")
    ncua_count = sum(1 for i in institutions if i["source"] == "ncua")
    ncua_zips  = sorted(CACHE_DIR.glob("call-report-data*.zip"))
    as_of      = get_data_as_of()

    if ncua_count == 0:
        warnings.append(
            "NCUA credit union data is empty — auto-download may have failed and no local "
            "call-report-data ZIP was found. Check network access to ncua.gov."
        )

    result = {
        "success": True,
        "total_records": len(institutions),
        "fdic_banks": fdic_count,
        "ncua_credit_unions": ncua_count,
        "data_as_of": as_of,
        "sources_refreshed": {
            "fdic":  f"Live from FDIC BankFind API — latest quarter auto-discovered (as of {as_of.get('fdic') or 'unknown'})",
            "ncua":  f"Auto-downloaded latest quarter: {ncua_zips[-1].name} (as of {as_of.get('ncua') or 'unknown'})" if ncua_zips else "Skipped — download failed and no local ZIP",
            "ffiec": f"Read from {ffiec_zips[-1].name} (manual; as of {as_of.get('ffiec') or 'unknown'})" if ffiec_zips else "Skipped — no ZIP found",
        },
    }

    if warnings:
        result["warnings"] = warnings

    return result


# ---------------------------------------------------------------------------
# Tool 5b: refresh_if_changed
# ---------------------------------------------------------------------------

@mcp.tool()
async def refresh_if_changed() -> dict:
    """
    Conditionally refresh the snapshot: rebuild ONLY if an upstream source changed
    (FFIEC ZIP content, or a newly published FDIC/NCUA quarter). If nothing changed,
    the expensive reprocessing is skipped. This is the cost-effective tool for
    scheduled (e.g. monthly) maintenance — unlike refresh_cache, which always rebuilds.

    Returns:
        Summary with `changed` (bool), the reason, per-source data_as_of dates, and counts.
    """
    from data_loader import refresh_if_changed as _refresh

    result = await _refresh()
    insts = get_all_institutions()
    result["total_records"]      = len(insts)
    result["fdic_banks"]         = sum(1 for i in insts if i["source"] == "fdic")
    result["ncua_credit_unions"] = sum(1 for i in insts if i["source"] == "ncua")
    return result


# ---------------------------------------------------------------------------
# Tool 6: get_top_institutions
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_top_institutions(
    top_n: int = 50,
    institution_type: str = "all",
    rank_by: str = "deposit_accounts",
) -> dict:
    """
    Return the top N US financial institutions ranked by size, with market share calculations.
    Use this tool when asked about the largest institutions, market coverage, or deposit share.
    Do NOT use search_institutions for ranking or market share questions — use this tool instead.

    Args:
        top_n: Number of top institutions to return (default 50, max 8700)
        institution_type: Filter by type — "bank", "cu" (credit union), or "all" (default)
        rank_by: Field to rank by — "deposit_accounts" (default)

    Returns:
        Ranked list of institutions with individual and cumulative market share percentages.
    """
    institutions = get_all_institutions()
    if not institutions:
        return {"error": "Data snapshot not loaded."}

    top_n = min(top_n, 8700)

    if institution_type == "bank":
        pool = [i for i in institutions if i["source"] == "fdic"]
    elif institution_type == "cu":
        pool = [i for i in institutions if i["source"] == "ncua"]
    else:
        pool = institutions

    def parse_int(val):
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    ranked = []
    for inst in pool:
        val = parse_int(inst.get(rank_by))
        if val is not None and val > 0:
            ranked.append((val, inst))

    if not ranked:
        return {"error": f"No institutions have numeric data for field '{rank_by}'."}

    ranked.sort(key=lambda x: x[0], reverse=True)

    total = sum(v for v, _ in ranked)
    top = ranked[:top_n]
    top_total = sum(v for v, _ in top)

    results = []
    cumulative = 0
    for rank, (val, inst) in enumerate(top, start=1):
        cumulative += val
        is_cu = inst["source"] == "ncua"
        results.append({
            "rank": rank,
            "name": inst.get("name", ""),
            "type": "Credit Union" if is_cu else "Bank",
            "city": inst.get("city", ""),
            "state": inst.get("state", ""),
            "fdic_cert": inst.get("cert", "") if not is_cu else None,
            "ncua_charter": inst.get("charter_number", "") if is_cu else None,
            "rssdid": inst.get("rssdid", "") or None,
            "deposit_accounts": val,
            "market_share_pct": round(val / total * 100, 3) if total else None,
            "cumulative_share_pct": round(cumulative / total * 100, 3) if total else None,
        })

    return {
        "ranked_by": rank_by,
        "institution_type_filter": institution_type,
        "total_institutions_with_data": len(ranked),
        "total_deposit_accounts_universe": total,
        "top_n_deposit_accounts": top_total,
        "top_n_market_share_pct": round(top_total / total * 100, 3) if total else None,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Tool 7: export_institutions
# ---------------------------------------------------------------------------

@mcp.tool()
async def export_institutions(
    output_path: str = "",
    institution_type: str = "all",
    state: str = "",
    sort_by: str = "deposit_accounts",
    sort_order: str = "desc",
    min_deposit_accounts: int = 0,
    top_n: int = 0,
) -> dict:
    """
    Export the full institution dataset to a CSV file on disk.
    Use this when the user wants a file, spreadsheet, or full data export.
    Do NOT use search_institutions or get_top_institutions for export requests — use this tool instead.

    Args:
        output_path: Full file path for the CSV (e.g. "/Users/nanievas/Desktop/fi_export.csv").
                     Defaults to ~/Desktop/fi_institutions_export.csv if not specified.
        institution_type: Filter by type — "bank", "cu" (credit union), or "all" (default)
        state: Optional 2-letter state filter (e.g. "UT"). Leave blank for all states.
        sort_by: Field to sort by — "deposit_accounts" (default), "name", "state"
        sort_order: "desc" (default, largest first) or "asc"
        min_deposit_accounts: Only include institutions with at least this many deposit accounts (default 0 = all)
        top_n: If > 0, only export the top N institutions after sorting (default 0 = all)

    Returns:
        Summary of the export including file path, row count, and applied filters.
    """
    import csv
    from pathlib import Path

    institutions = get_all_institutions()
    if not institutions:
        return {"error": "Data snapshot not loaded."}

    if not output_path.strip():
        output_path = str(Path.home() / "Desktop" / "fi_institutions_export.csv")

    if institution_type == "bank":
        pool = [i for i in institutions if i["source"] == "fdic"]
    elif institution_type == "cu":
        pool = [i for i in institutions if i["source"] == "ncua"]
    else:
        pool = list(institutions)

    if state:
        state_upper = state.upper()
        state_full_map = {
            "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
            "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
            "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
            "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
            "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
            "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
            "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
            "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
            "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
            "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
            "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
            "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
            "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
        }
        state_full = state_full_map.get(state_upper, "")
        pool = [
            i for i in pool
            if i.get("state", "").upper() == state_upper
            or i.get("state", "") == state_full
        ]

    def parse_int(val):
        try:
            return int(val)
        except (ValueError, TypeError):
            return 0

    if min_deposit_accounts > 0:
        pool = [i for i in pool if parse_int(i.get("deposit_accounts")) >= min_deposit_accounts]

    reverse = sort_order.lower() != "asc"
    if sort_by == "deposit_accounts":
        pool.sort(key=lambda i: parse_int(i.get("deposit_accounts")), reverse=reverse)
    elif sort_by == "name":
        pool.sort(key=lambda i: i.get("name", "").lower(), reverse=reverse)
    elif sort_by == "state":
        pool.sort(key=lambda i: i.get("state", "").lower(), reverse=reverse)

    if top_n > 0:
        pool = pool[:top_n]

    all_deposit_total = sum(parse_int(i.get("deposit_accounts")) for i in get_all_institutions())

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "rank", "name", "type", "source", "regulator", "insured_by", "city", "state",
        "fdic_cert", "ncua_charter", "rssdid", "aba_routing",
        "deposit_accounts", "market_share_pct", "web_address",
        "charter_type", "charter_type_desc", "inst_category",
    ]

    # Human-readable NCUA charter-type labels (credit unions only).
    charter_type_labels = {
        "1": "Federally Chartered Credit Union (FCU)",
        "2": "State Chartered, Federally Insured (FISCU)",
        "3": "State Chartered, Privately Insured",
    }

    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for rank, inst in enumerate(pool, start=1):
            dep = parse_int(inst.get("deposit_accounts"))
            is_cu = inst["source"] == "ncua"
            writer.writerow({
                "rank": rank,
                "name": inst.get("name", ""),
                "type": "Credit Union" if is_cu else "Bank",
                "source": inst["source"],
                "regulator": "NCUA" if is_cu else "FDIC / OCC / Federal Reserve",
                "insured_by": "NCUA (NCUSIF)" if is_cu else "FDIC",
                "city": inst.get("city", ""),
                "state": inst.get("state", ""),
                "fdic_cert": inst.get("cert", "") if not is_cu else "",
                "ncua_charter": inst.get("charter_number", "") if is_cu else "",
                "rssdid": inst.get("rssdid", "") or "",
                "aba_routing": inst.get("aba_routing", "") or "",
                "deposit_accounts": dep if dep else "",
                "market_share_pct": round(dep / all_deposit_total * 100, 4) if all_deposit_total and dep else "",
                "web_address": inst.get("web_address", "") or "",
                "charter_type": inst.get("charter_type", "") or "",
                "charter_type_desc": charter_type_labels.get(inst.get("charter_type", ""), "") if is_cu else "",
                "inst_category": inst.get("inst_category", "") or "",
            })

    return {
        "success": True,
        "file": str(output_file),
        "rows_exported": len(pool),
        "filters_applied": {
            "institution_type": institution_type,
            "state": state or "all",
            "min_deposit_accounts": min_deposit_accounts,
            "top_n": top_n if top_n > 0 else "all",
            "sort_by": sort_by,
            "sort_order": sort_order,
        },
    }


# ---------------------------------------------------------------------------
# Tool 8: get_institution_history  ← NEW
# ---------------------------------------------------------------------------

def _resolve_name(rssd: str, institutions: list[dict]) -> str:
    """
    Look up an institution name by RSSD ID.
    Checks active institutions first, then falls back to the NIC
    name lookup which covers both active and closed/historical institutions.
    """
    if not rssd:
        return "Unknown"

    # Try active institutions first
    for inst in institutions:
        if inst.get("rssdid", "").strip() == rssd:
            return inst.get("name", f"RSSD {rssd}")

    # Fall back to NIC name lookup (covers defunct institutions)
    nic_names = get_nic_names()

    entry = nic_names.get(rssd)
    if entry:
        city  = entry.get("city", "")
        state = entry.get("state", "")
        name  = entry.get("name", "")
        location = f" — {city}, {state}" if city or state else ""
        return f"{name}{location} (closed)"

    return f"RSSD {rssd} (not in dataset)"


def _build_history_summary(
    name: str,
    predecessors: list[dict],
    successors: list[dict],
    parent_rssd: str | None,
    parent_name: str | None,
    subsidiary_count: int,
) -> str:
    """Return a plain-English one-paragraph summary of the institution's history."""
    parts = []

    if parent_rssd and parent_name:
        parts.append(f"{name} is a subsidiary of {parent_name}.")

    if predecessors:
        pred_names = [p["name"] for p in predecessors[:3]]
        overflow = f" (and {len(predecessors) - 3} more)" if len(predecessors) > 3 else ""
        parts.append(
            f"{name} absorbed {', '.join(pred_names)}{overflow} through merger or acquisition."
        )

    if successors:
        s = successors[0]
        parts.append(
            f"{name} was {s['event_type'].lower()} into {s['name']} on {s['event_date']}."
        )

    if subsidiary_count:
        parts.append(f"{name} has {subsidiary_count} known subsidiary or affiliate(s).")

    if not parts:
        parts.append(
            f"No merger, acquisition, rebrand, or parent/subsidiary history found for {name}."
        )

    return " ".join(parts)


@mcp.tool()
async def get_institution_history(rssd_id: str) -> dict:
    """
    Return the full merger, acquisition, and rebrand lineage for a financial institution.

    Given an RSSD ID, this tool returns:
    - The institution's current profile (name, type, state)
    - All predecessor institutions (entities that merged into or were acquired by this one)
    - All successor institutions (what this institution became, if it was acquired or merged away)
    - Parent company (if this institution is owned by another entity)
    - Subsidiary institutions it controls
    - A plain-English summary of the lineage

    Use crosswalk_identifiers or search_institutions first if you only have an FDIC cert,
    NCUA charter, or institution name and need to find the RSSD ID.

    Args:
        rssd_id: The RSSD ID of the institution (string or integer — both work)

    Returns:
        Dict with predecessor, successor, parent, and subsidiary history plus a plain-English summary.
    """
    rssd_id = str(rssd_id).strip()
    institutions = get_all_institutions()

    if not institutions:
        return {"error": "Data snapshot not loaded."}

    # Find the institution record
    inst = next(
        (i for i in institutions if i.get("rssdid", "").strip() == rssd_id and rssd_id not in ("", "0")),
        None,
    )

    if not inst:
        return {
            "error": (
                f"No institution found with RSSD ID '{rssd_id}'. "
                "Use search_institutions or crosswalk_identifiers to find the correct RSSD ID."
            )
        }

    name     = inst.get("name", "Unknown")
    is_cu    = inst["source"] == "ncua"

    # Pull NIC-enriched fields (empty lists/None if NIC ZIP was not loaded)
    raw_predecessors = inst.get("predecessors", [])   # events where this inst is the successor
    raw_successors   = inst.get("successors", [])     # events where this inst is the predecessor
    parent_rssd      = inst.get("parent_rssd")
    raw_subsidiaries = inst.get("subsidiaries", [])

    # Build predecessor list
    predecessors = []
    for event in raw_predecessors:
        pred_rssd = event.get("predecessor_rssd", "")
        predecessors.append({
            "rssd_id":    pred_rssd,
            "name":       _resolve_name(pred_rssd, institutions),
            "event_type": event.get("transformation_type", "Unknown"),
            "event_date": event.get("transformation_date", ""),
        })

    # Build successor list
    successors = []
    for event in raw_successors:
        succ_rssd = event.get("successor_rssd", "")
        successors.append({
            "rssd_id":    succ_rssd,
            "name":       _resolve_name(succ_rssd, institutions),
            "event_type": event.get("transformation_type", "Unknown"),
            "event_date": event.get("transformation_date", ""),
        })

    # Resolve parent name
    parent_name = _resolve_name(parent_rssd, institutions) if parent_rssd else None

    # Build subsidiary list (capped at 30 to keep response manageable)
    CAP = 30
    subsidiaries = []
    for sub_rssd in raw_subsidiaries[:CAP]:
        subsidiaries.append({
            "rssd_id": sub_rssd,
            "name":    _resolve_name(sub_rssd, institutions),
        })
    overflow_note = None
    if len(raw_subsidiaries) > CAP:
        overflow_note = f"{len(raw_subsidiaries) - CAP} additional subsidiaries not shown."

    nic_loaded = bool(raw_predecessors or raw_successors or parent_rssd or raw_subsidiaries)

    return {
        "rssd_id":   rssd_id,
        "name":      name,
        "type":      "Credit Union" if is_cu else "Bank / Thrift",
        "state":     inst.get("state", ""),
        "city":      inst.get("city", ""),
        "fdic_cert":     inst.get("cert") if not is_cu else None,
        "ncua_charter":  inst.get("charter_number") if is_cu else None,
        "nic_data_loaded": nic_loaded,
        "parent": {
            "rssd_id": parent_rssd,
            "name":    parent_name,
        } if parent_rssd else None,
        "predecessors":      predecessors,
        "successors":        successors,
        "subsidiaries":      subsidiaries,
        "subsidiaries_overflow": overflow_note,
        "summary": _build_history_summary(
            name, predecessors, successors, parent_rssd, parent_name, len(raw_subsidiaries)
        ),
    }
# ---------------------------------------------------------------------------
# Tool 9: get_recent_changes
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_recent_changes(
    days: int = 365,
    institution_type: str = "all",
    event_type: str = "all",
    state: str = "",
    check_portals: bool = True,
    max_portal_checks: int = 50,
) -> dict:
    """
    Return recent merger, acquisition, failure, and restructuring events
    from the FFIEC NIC Transformations data. Use this to identify institutions
    that have changed status and may need dataset updates.

    Each event lists the FULL metadata of both the predecessor and successor
    institution (name, type, regulator, location, identifiers, deposit accounts,
    and web address). When check_portals is on, the tool also fetches each
    predecessor's home/login URL to determine whether it is still operating
    independently or has been consumed by the acquirer.

    Args:
        days: How many days back to look (default 365 = last year, max 3650)
        institution_type: Filter by "bank", "cu" (credit union), or "all" (default)
        event_type: Filter by "merger", "failure", "split", "rebrand", or "all" (default)
        state: Optional 2-letter state abbreviation to narrow results (e.g. "UT")
        check_portals: If True (default), fetch each predecessor's web portal and classify it as
                       still-independent vs. consumed by the acquirer (adds network latency).
        max_portal_checks: Cap on how many predecessor portals to fetch (default 50, most-recent
                           events first) to bound latency. Set check_portals=False to skip entirely.

    Returns:
        Summary of changes grouped by event type, each with full predecessor/successor metadata
        and (when enabled) a portal_status verdict on the predecessor.
    """
    import zipfile
    import csv
    import io
    import asyncio
    import httpx
    from urllib.parse import urlparse
    from datetime import datetime, timedelta
    from data_loader import CACHE_DIR

    days = min(days, 3650)
    max_portal_checks = max(0, min(max_portal_checks, 500))
    cutoff_date = (datetime.today() - timedelta(days=days)).strftime("%Y%m%d")

    def reg_domain(url: str) -> str:
        """Registered domain (last two labels, www stripped) for redirect comparison."""
        if not url:
            return ""
        u = url.strip()
        if not u.startswith(("http://", "https://")):
            u = "http://" + u
        host = (urlparse(u).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    async def check_portal(client, sem, pred_url, succ_domain):
        """Fetch a predecessor's portal and classify independent vs. consumed."""
        own_domain = reg_domain(pred_url)
        target = pred_url.strip()
        if not target.startswith(("http://", "https://")):
            target = "https://" + target
        async with sem:
            try:
                resp = await client.get(target)
            except Exception as e:
                return {
                    "checked": True, "reachable": False, "http_status": None,
                    "final_url": None, "verdict": "unreachable",
                    "note": f"portal did not respond ({type(e).__name__}) — likely retired",
                }
        final_url = str(resp.url)
        final_domain = reg_domain(final_url)
        if succ_domain and final_domain == succ_domain:
            verdict, note = "consumed_by_acquirer", "redirects to the acquirer's domain"
        elif final_domain and final_domain == own_domain:
            verdict, note = "independent_portal_live", "still served on its own domain"
        elif not final_domain:
            verdict, note = "unknown", "could not determine final domain"
        else:
            verdict, note = "redirects_elsewhere", f"redirects to {final_domain}"
        return {
            "checked": True, "reachable": True, "http_status": resp.status_code,
            "final_url": final_url, "verdict": verdict, "note": note,
        }

    # Find transformations ZIP
    trans_zip = CACHE_DIR / "CSV_TRANSFORMATIONS.zip"
    if not trans_zip.exists():
        return {"error": "CSV_TRANSFORMATIONS.zip not found in cache/. Cannot query recent changes."}

    # Load name lookups and build an RSSD -> institution index ONCE.
    # The old implementation linear-scanned all ~8,600 institutions on every
    # resolve()/inst_type()/inst_state() call (several per event) — O(events ×
    # institutions). The index makes every lookup O(1).
    institutions = get_all_institutions()
    nic_names = get_nic_names()

    by_rssd: dict[str, dict] = {}
    for _inst in institutions:
        _r = _inst.get("rssdid", "").strip()
        if _r and _r != "0" and _r not in by_rssd:
            by_rssd[_r] = _inst

    def resolve(rssd: str) -> str:
        if not rssd:
            return "Unknown"
        inst = by_rssd.get(rssd)
        if inst:
            return inst.get("name", f"RSSD {rssd}")
        entry = nic_names.get(rssd)
        if entry:
            name  = entry.get("name", "")
            city  = entry.get("city", "")
            state = entry.get("state", "")
            loc   = f" — {city}, {state}" if city or state else ""
            return f"{name}{loc} (closed)"
        return f"RSSD {rssd}"

    def inst_type(rssd: str) -> str:
        """Guess institution type from active dataset or NIC names."""
        inst = by_rssd.get(rssd)
        if inst:
            return "cu" if inst["source"] == "ncua" else "bank"
        entry = nic_names.get(rssd)
        if entry:
            name = entry.get("name", "").upper()
            if any(w in name for w in ("FCU", " CU", "CREDIT UNION")):
                return "cu"
        return "bank"

    def meta(rssd: str) -> dict:
        """Full metadata for an institution by RSSD, active record or NIC fallback."""
        if not rssd:
            return {"rssd_id": "", "name": "Unknown", "status": "unknown"}
        inst = by_rssd.get(rssd)
        if inst:
            is_cu = inst["source"] == "ncua"
            return {
                "rssd_id":          rssd,
                "name":             inst.get("name", ""),
                "type":             "Credit Union" if is_cu else "Bank / Thrift",
                "regulator":        "NCUA" if is_cu else "FDIC / OCC / Federal Reserve",
                "city":             inst.get("city", ""),
                "state":            inst.get("state", ""),
                "fdic_cert":        inst.get("cert", "") if not is_cu else "",
                "ncua_charter":     inst.get("charter_number", "") if is_cu else "",
                "aba_routing":      inst.get("aba_routing", "") or "",
                "deposit_accounts": inst.get("deposit_accounts", "") or "",
                "web_address":      (inst.get("web_address", "") or "").strip(),
                "status":           "in_active_dataset",
            }
        entry = nic_names.get(rssd)
        if entry:
            return {
                "rssd_id":     rssd,
                "name":        entry.get("name", f"RSSD {rssd}"),
                "type":        "Credit Union" if inst_type(rssd) == "cu" else "Bank / Thrift",
                "city":        entry.get("city", ""),
                "state":       entry.get("state", ""),
                "web_address": "",
                "status":      "closed_historical",
            }
        return {"rssd_id": rssd, "name": f"RSSD {rssd}", "status": "not_in_dataset"}

    TRNSFM_LABELS = {
        "1":  "Merger",
        "2":  "Acquisition",
        "3":  "Charter Change",
        "4":  "Failed / Assisted",
        "5":  "Name Change / Rebrand",
        "6":  "Split-Off",
        "7":  "Split",
        "8":  "New Establishment",
        "9":  "Dissolution",
        "10": "Charter Number Change",
        "11": "Ceased Operations",
        "50": "Failed / FDIC-Assisted Acquisition",
    }

    EVENT_TYPE_MAP = {
        "merger":  {"1", "2"},
        "failure": {"4", "50"},
        "split":   {"6", "7"},
        "rebrand": {"5", "3", "10"},
    }

    # State abbreviation → full name for CU state matching
    STATE_FULL = {
        "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
        "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
        "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
        "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
        "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
        "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
        "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
        "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
        "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
        "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
        "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
        "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
        "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
    }

    # Normalize the state filter so "Utah" works the same as "UT".
    # inst_state() returns abbreviations, so resolve a full name to its abbreviation.
    state_filter = ""
    if state:
        s = state.strip()
        if len(s) == 2:
            state_filter = s.upper()
        else:
            full_to_abbr = {full.upper(): abbr for abbr, full in STATE_FULL.items()}
            state_filter = full_to_abbr.get(s.upper(), s.upper())

    full_to_abbr = {full: abbr for abbr, full in STATE_FULL.items()}

    def inst_state(rssd: str) -> str:
        inst = by_rssd.get(rssd)
        if inst:
            st = inst.get("state", "")
            return full_to_abbr.get(st, st.upper())
        entry = nic_names.get(rssd)
        if entry:
            return entry.get("state", "").upper()
        return ""

    # Read and filter transformations
    with zipfile.ZipFile(trans_zip) as z:
        csv_name = next(n for n in z.namelist() if n.upper().endswith(".CSV"))
        content  = z.read(csv_name).decode("latin-1")

    rows = list(csv.DictReader(io.StringIO(content)))
    rows = [r for r in rows if r.get("DT_TRANS", "") >= cutoff_date]

    # Apply event_type filter
    if event_type != "all":
        allowed_codes = EVENT_TYPE_MAP.get(event_type.lower(), set())
        rows = [r for r in rows if r.get("TRNSFM_CD", "").strip() in allowed_codes]

    # Apply institution_type and state filters
    filtered = []
    for r in rows:
        pred_rssd = r.get("#ID_RSSD_PREDECESSOR", "").strip()
        succ_rssd = r.get("ID_RSSD_SUCCESSOR", "").strip()

        if institution_type != "all":
            pred_type = inst_type(pred_rssd)
            if pred_type != institution_type:
                continue

        if state_filter:
            pred_state  = inst_state(pred_rssd)
            succ_state  = inst_state(succ_rssd)
            if state_filter not in (pred_state, succ_state):
                continue

        filtered.append(r)

    filtered.sort(key=lambda r: r["DT_TRANS"], reverse=True)

    # Build enriched records — full metadata for both predecessor and successor.
    records = []
    for r in filtered:
        code      = r.get("TRNSFM_CD", "").strip()
        date_raw  = r.get("DT_TRANS", "").strip()
        date_fmt  = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}" if len(date_raw) == 8 else date_raw
        pred_rssd = r.get("#ID_RSSD_PREDECESSOR", "").strip()
        succ_rssd = r.get("ID_RSSD_SUCCESSOR", "").strip()

        records.append({
            "date":         date_fmt,
            "event_type":   TRNSFM_LABELS.get(code, f"Type {code}"),
            "event_code":   code,
            "predecessor":  meta(pred_rssd),
            "successor":    meta(succ_rssd),
        })

    # ── Portal checks: is each predecessor still independent or consumed? ──────
    # Runs concurrently, capped at max_portal_checks (most-recent events first).
    portal_summary = {
        "independent_portal_live": 0, "consumed_by_acquirer": 0,
        "redirects_elsewhere": 0, "unreachable": 0, "unknown": 0,
        "no_url_on_record": 0, "not_checked": 0,
    }

    if check_portals and max_portal_checks > 0:
        to_check = []
        for rec in records:
            pred = rec["predecessor"]
            url  = pred.get("web_address", "")
            if not url:
                pred["portal_status"] = {"checked": False, "verdict": "no_url_on_record",
                                         "note": "no web address on record for the predecessor"}
                portal_summary["no_url_on_record"] += 1
            elif len(to_check) >= max_portal_checks:
                pred["portal_status"] = {"checked": False, "verdict": "not_checked",
                                         "note": f"skipped: max_portal_checks ({max_portal_checks}) reached"}
                portal_summary["not_checked"] += 1
            else:
                to_check.append((pred, url, reg_domain(rec["successor"].get("web_address", ""))))

        if to_check:
            sem     = asyncio.Semaphore(20)
            timeout = httpx.Timeout(8.0)
            headers = {"User-Agent": "Mozilla/5.0 (fi-lookup-mcp portal check)"}
            # verify=False so a redirect to the acquirer is still detected even
            # behind an expired/abandoned certificate on the old domain.
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout,
                                         headers=headers, verify=False) as client:
                results = await asyncio.gather(
                    *[check_portal(client, sem, url, succ_dom) for (_p, url, succ_dom) in to_check]
                )
            for (pred, _u, _d), res in zip(to_check, results):
                pred["portal_status"] = res
                portal_summary[res["verdict"]] = portal_summary.get(res["verdict"], 0) + 1
    else:
        for rec in records:
            rec["predecessor"]["portal_status"] = {"checked": False, "verdict": "not_checked",
                                                   "note": "portal checking disabled"}
            portal_summary["not_checked"] += 1

    # Group by event type (records are already sorted most-recent first).
    groups: dict[str, list] = {"failures": [], "mergers": [], "rebrands": [], "splits": [], "other": []}
    for rec in records:
        code = rec["event_code"]
        if code in {"4", "50"}:
            groups["failures"].append(rec)
        elif code in {"1", "2"}:
            groups["mergers"].append(rec)
        elif code in {"5", "3", "10"}:
            groups["rebrands"].append(rec)
        elif code in {"6", "7"}:
            groups["splits"].append(rec)
        else:
            groups["other"].append(rec)

    total = len(records)

    return {
        "query": {
            "days":             days,
            "since":            f"{cutoff_date[:4]}-{cutoff_date[4:6]}-{cutoff_date[6:]}",
            "institution_type": institution_type,
            "event_type":       event_type,
            "state":            state or "all",
            "check_portals":    check_portals,
            "max_portal_checks": max_portal_checks if check_portals else 0,
        },
        "summary": {
            "total_events":  total,
            "failures":      len(groups["failures"]),
            "mergers":       len(groups["mergers"]),
            "rebrands":      len(groups["rebrands"]),
            "splits":        len(groups["splits"]),
            "other":         len(groups["other"]),
        },
        "portal_summary": portal_summary,
        "portal_status_legend": {
            "independent_portal_live": "Predecessor URL still served on its own domain — appears to operate independently.",
            "consumed_by_acquirer":    "Predecessor URL redirects to the acquirer's domain — folded in.",
            "redirects_elsewhere":     "Redirects to a third domain (e.g. a rebrand or division site) — review.",
            "unreachable":             "Portal did not respond — likely retired.",
            "no_url_on_record":        "No web address on record to check.",
            "not_checked":             "Skipped (cap reached or checking disabled).",
        },
        "failures":  groups["failures"],
        "mergers":   groups["mergers"],
        "rebrands":  groups["rebrands"],
        "splits":    groups["splits"],
        "other":     groups["other"],
    }

# ---------------------------------------------------------------------------
# Tool 10: list_institutions  ← NEW
# ---------------------------------------------------------------------------

# Full canonical metadata projection for one institution record.
# Keeps every field, derives the credit-union charter-type label, and flattens
# the NIC lineage lists to counts (use get_institution_history for the detail).
_LIST_NUMERIC_FIELDS = {
    "deposit_accounts", "total_assets", "commercial_loans_000",
    "predecessor_count", "successor_count", "subsidiary_count",
}


def _full_record(inst: dict) -> dict:
    """Return every metadata field for an institution in a flat, uniform shape."""
    is_cu = inst["source"] == "ncua"
    charter_type_desc = {
        "1": "Federally Chartered Credit Union (FCU)",
        "2": "State Chartered, Federally Insured (FISCU)",
        "3": "State Chartered, Privately Insured",
    }.get(inst.get("charter_type", ""), "") if is_cu else ""

    return {
        "name":              inst.get("name", ""),
        "type":              "Credit Union" if is_cu else "Bank / Thrift",
        "source":            inst["source"],
        "regulator":         "NCUA" if is_cu else "FDIC / OCC / Federal Reserve",
        "city":              inst.get("city", ""),
        "state":             inst.get("state", ""),
        "fdic_cert":         inst.get("cert", "") if not is_cu else "",
        "ncua_charter":      inst.get("charter_number", "") if is_cu else "",
        "rssdid":            inst.get("rssdid", "") or "",
        "aba_routing":       inst.get("aba_routing", "") or "",
        "deposit_accounts":  inst.get("deposit_accounts", "") or "",
        "total_assets":      inst.get("total_assets", "") or "",
        "web_address":       inst.get("web_address", "") or "",
        "charter_type":      inst.get("charter_type", "") or "",
        "charter_type_desc": charter_type_desc,
        "inst_category":     inst.get("inst_category", "") or "",
        "parent_rssd":            inst.get("parent_rssd") or "",
        "predecessor_count":      len(inst.get("predecessors", []) or []),
        "successor_count":        len(inst.get("successors", []) or []),
        "subsidiary_count":       len(inst.get("subsidiaries", []) or []),
        "business_lending":       inst.get("business_lending", "") or "unknown",
        "small_business_lending": inst.get("small_business_lending", "") or "unknown",
        "commercial_loans_000":   inst.get("commercial_loans_000", 0) or 0,
        "data_as_of":             inst.get("data_as_of", "") or "",
    }


# Field names a record exposes — used to validate sort_by / search_fields / fields.
_LIST_FIELDS = list(_full_record({"source": "fdic"}).keys())


@mcp.tool()
async def list_institutions(
    search: str = "",
    search_fields: str = "name",
    institution_type: str = "all",
    state: str = "",
    min_deposit_accounts: int = 0,
    max_deposit_accounts: int = 0,
    has_routing: bool = False,
    has_rssd: bool = False,
    has_history: bool = False,
    sort_by: str = "name",
    sort_order: str = "asc",
    limit: int = 100,
    offset: int = 0,
    fields: str = "all",
    export_path: str = "",
    export_format: str = "csv",
) -> dict:
    """
    Pull the full institution list with ALL metadata fields, then search, filter, sort,
    and optionally export it. This is the general-purpose browse/query/export tool over the
    complete FDIC + NCUA dataset.

    Use this when the user wants to "list", "browse", "show all", "filter", or "export" the
    dataset with arbitrary criteria. For fuzzy name lookup of a single institution use
    search_institutions; for deposit rankings/market share use get_top_institutions.

    Available metadata fields (also the valid values for sort_by, search_fields, and fields):
      name, type, source, regulator, city, state, fdic_cert, ncua_charter, rssdid,
      aba_routing, deposit_accounts, total_assets, web_address, charter_type,
      charter_type_desc, inst_category, parent_rssd, predecessor_count,
      successor_count, subsidiary_count

    Args:
        search: Case-insensitive substring to match (empty = no text filter).
        search_fields: Comma-separated fields to search within, or "all" for every text field.
                       Default "name". Example: "name,city".
        institution_type: "bank", "cu" (credit union), or "all" (default).
        state: 2-letter abbrev or full name (e.g. "UT" or "Utah"). Blank = all states.
        min_deposit_accounts: Keep only institutions with at least this many deposit accounts (0 = no min).
        max_deposit_accounts: Keep only institutions with at most this many deposit accounts (0 = no max).
        has_routing: If True, keep only institutions that have an ABA routing number.
        has_rssd: If True, keep only institutions that have an RSSD ID.
        has_history: If True, keep only institutions with NIC lineage (predecessor/successor/subsidiary).
        sort_by: Field to sort by (default "name"). Numeric fields sort numerically.
        sort_order: "asc" (default) or "desc".
        limit: Max rows to return inline (default 100, max 1000). Ignored when exporting.
        offset: Number of matched rows to skip before returning (for pagination, default 0).
        fields: "all" (default) for every metadata field, or a comma-separated subset to project.
        export_path: If set, write ALL matched rows (not just the inline page) to this file and
                     return a summary instead of inline rows. Defaults under ~/Desktop if a bare
                     filename is given.
        export_format: "csv" (default) or "json". Only used when export_path is set.

    Returns:
        When not exporting: dict with total_matched, applied query, pagination, and the rows page.
        When exporting: dict with the file path, row count, format, and applied query.
    """
    import csv
    import json as _json
    from pathlib import Path

    institutions = get_all_institutions()
    if not institutions:
        return {"error": "Data snapshot not loaded. Server may still be starting up."}

    # ── Validate field-name arguments ────────────────────────────────────────
    if sort_by not in _LIST_FIELDS:
        return {"error": f"Invalid sort_by '{sort_by}'. Valid fields: {', '.join(_LIST_FIELDS)}"}

    if search_fields.strip().lower() == "all":
        active_search_fields = list(_LIST_FIELDS)
    else:
        active_search_fields = [f.strip() for f in search_fields.split(",") if f.strip()]
        bad = [f for f in active_search_fields if f not in _LIST_FIELDS]
        if bad:
            return {"error": f"Invalid search_fields {bad}. Valid fields: {', '.join(_LIST_FIELDS)}"}

    if fields.strip().lower() == "all":
        projection = list(_LIST_FIELDS)
    else:
        projection = [f.strip() for f in fields.split(",") if f.strip()]
        bad = [f for f in projection if f not in _LIST_FIELDS]
        if bad:
            return {"error": f"Invalid fields {bad}. Valid fields: {', '.join(_LIST_FIELDS)}"}

    # ── Type filter ──────────────────────────────────────────────────────────
    if institution_type == "bank":
        pool = [i for i in institutions if i["source"] == "fdic"]
    elif institution_type == "cu":
        pool = [i for i in institutions if i["source"] == "ncua"]
    else:
        pool = list(institutions)

    # ── State filter (FDIC stores full names, NCUA stores 2-letter codes) ─────
    if state:
        state_upper = state.upper()
        state_full_map = {
            "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
            "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
            "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
            "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
            "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
            "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
            "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
            "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
            "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
            "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
            "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
            "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
            "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
        }
        # Accept either an abbreviation or a full name on input.
        if len(state) == 2:
            state_full = state_full_map.get(state_upper, "")
        else:
            state_full = state.title()
            abbr = {v.upper(): k for k, v in state_full_map.items()}.get(state_upper, "")
            state_upper = abbr or state_upper
        pool = [
            i for i in pool
            if i.get("state", "").upper() == state_upper
            or i.get("state", "") == state_full
        ]

    # ── Project to full records, then apply value filters ─────────────────────
    def parse_int(val):
        try:
            return int(val)
        except (ValueError, TypeError):
            return 0

    records = [_full_record(i) for i in pool]

    if search.strip():
        needle = search.strip().lower()
        records = [
            r for r in records
            if any(needle in str(r.get(f, "")).lower() for f in active_search_fields)
        ]

    if min_deposit_accounts > 0:
        records = [r for r in records if parse_int(r["deposit_accounts"]) >= min_deposit_accounts]
    if max_deposit_accounts > 0:
        records = [r for r in records if parse_int(r["deposit_accounts"]) <= max_deposit_accounts]
    if has_routing:
        records = [r for r in records if r["aba_routing"]]
    if has_rssd:
        records = [r for r in records if r["rssdid"]]
    if has_history:
        records = [
            r for r in records
            if r["predecessor_count"] or r["successor_count"] or r["subsidiary_count"] or r["parent_rssd"]
        ]

    # ── Sort ──────────────────────────────────────────────────────────────────
    reverse = sort_order.lower() != "asc"
    if sort_by in _LIST_NUMERIC_FIELDS:
        records.sort(key=lambda r: parse_int(r[sort_by]), reverse=reverse)
    else:
        records.sort(key=lambda r: str(r.get(sort_by, "")).lower(), reverse=reverse)

    total_matched = len(records)

    applied_query = {
        "search":               search or None,
        "search_fields":        active_search_fields,
        "institution_type":     institution_type,
        "state":                state or "all",
        "min_deposit_accounts": min_deposit_accounts,
        "max_deposit_accounts": max_deposit_accounts or None,
        "has_routing":          has_routing,
        "has_rssd":             has_rssd,
        "has_history":          has_history,
        "sort_by":              sort_by,
        "sort_order":           sort_order,
    }

    # ── Export path: write ALL matched rows, return summary ───────────────────
    if export_path.strip():
        out = Path(export_path)
        if not out.is_absolute() and out.parent == Path("."):
            out = Path.home() / "Desktop" / out.name
        out.parent.mkdir(parents=True, exist_ok=True)

        fmt = export_format.strip().lower()
        if fmt == "json":
            ranked = [{"rank": n, **r} for n, r in enumerate(records, start=1)]
            tmp = out.with_suffix(out.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(ranked, f, indent=2)
            tmp.rename(out)
        elif fmt == "csv":
            fieldnames = ["rank"] + _LIST_FIELDS
            tmp = out.with_suffix(out.suffix + ".tmp")
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for n, r in enumerate(records, start=1):
                    writer.writerow({"rank": n, **r})
            tmp.rename(out)
        else:
            return {"error": f"Invalid export_format '{export_format}'. Use 'csv' or 'json'."}

        return {
            "success":       True,
            "exported":      True,
            "file":          str(out),
            "format":        fmt,
            "rows_exported": total_matched,
            "fields":        _LIST_FIELDS,
            "applied_query": applied_query,
        }

    # ── Inline path: project + paginate ───────────────────────────────────────
    limit = max(0, min(limit, 1000))
    offset = max(0, offset)
    page = records[offset:offset + limit]
    if projection != _LIST_FIELDS:
        page = [{f: r[f] for f in projection} for r in page]

    return {
        "total_matched": total_matched,
        "applied_query": applied_query,
        "pagination": {
            "offset":        offset,
            "limit":         limit,
            "returned":      len(page),
            "has_more":      offset + len(page) < total_matched,
            "next_offset":   offset + len(page) if offset + len(page) < total_matched else None,
        },
        "fields":  projection,
        "results": page,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()