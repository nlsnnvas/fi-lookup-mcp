"""
server.py
FI-Lookup MCP Server — financial institution lookup and reconciliation.
Public FDIC/NCUA data only. Not connected to any employer systems.
"""

from contextlib import asynccontextmanager
from fastmcp import FastMCP
from data_loader import build_snapshot, get_all_institutions
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
        "reconcile_institution (best-match scoring), crosswalk_identifiers (ID translation)."
    )
)


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


if __name__ == "__main__":
    mcp.run()

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
        # Add any remaining fields from the record not already included
        excluded = {"source", "cert", "charter_number", "total_assets", "aba_routing",
                    "rssdid", "name", "city", "state", "deposit_accounts",
                    "web_address", "charter_type", "inst_category"}
        for k, v in inst.items():
            if k not in excluded and v not in ("", None, "0", 0):
                profile[k] = v
        # Strip None and empty values
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

    # Find matching institution(s)
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
