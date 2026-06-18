"""
reconciler.py
Best-match scoring logic for resolving messy institution records
to canonical FDIC/NCUA entries.

Scoring components:
  - Name similarity (token-set ratio + Jaro-Winkler, abbreviation-aware)
  - Geographic agreement (city + state)
  - Identifier override (exact cert/charter/RSSD match scores 1.0)

This is a tool-use/reconciliation pattern — NOT RAG.
"""

import re
from rapidfuzz import fuzz, distance

# ---------------------------------------------------------------------------
# Abbreviation normalization
# ---------------------------------------------------------------------------

ABBREV_MAP = {
    r"\bfcu\b": "federal credit union",
    r"\bcu\b": "credit union",
    r"\bn\.?a\.?\b": "national association",
    r"\bnat'?l\b": "national",
    r"\bfed\.?\b": "federal",
    r"\bfsb\b": "federal savings bank",
    r"\bssb\b": "state savings bank",
    r"\bfsla\b": "federal savings and loan association",
    r"\bcorp\.?\b": "corporation",
    r"\bintl\b": "international",
    r"\bmtn\b": "mountain",
    r"\bmt\.?\b": "mountain",
    r"\bst\.?\b": "saint",
    r"\bsvc\.?s?\b": "services",
    r"\bcomm\.?\b": "community",
    r"\bfin\.?\b": "financial",
    r"\bamer\.?\b": "america",
    r"\bamr\b": "america",
    r"\bfirst\b": "first",
    r"\bnbk\b": "national bank",
    r"\bnb\b": "national bank",
    r"\bbnk\b": "bank",
    r"\bbk\b": "bank",
}

def normalize_name(name: str) -> str:
    """Lowercase, expand abbreviations, strip punctuation."""
    name = name.lower().strip()
    # Remove punctuation except spaces
    name = re.sub(r"[^\w\s]", " ", name)
    # Expand abbreviations
    for pattern, replacement in ABBREV_MAP.items():
        name = re.sub(pattern, replacement, name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_name(query_norm: str, candidate_norm: str) -> float:
    """Blend token-set ratio and Jaro-Winkler for robust name matching."""
    token_set = fuzz.token_set_ratio(query_norm, candidate_norm) / 100.0
    jaro = distance.JaroWinkler.normalized_similarity(query_norm, candidate_norm)
    # Weight token-set higher — it handles word-order and subset matches better
    return round(0.7 * token_set + 0.3 * jaro, 4)


def score_geo(
    query_city: str,
    query_state: str,
    candidate: dict,
) -> tuple[float, list[str]]:
    """
    Return (geo_score, reasons).
    State match = 0.6, city match = 0.4 (additive).
    FDIC uses full state names; NCUA uses 2-letter codes.
    We normalize both to 2-letter for comparison.
    """
    STATE_ABBREV = {
        "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
        "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
        "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
        "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
        "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
        "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
        "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
        "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
        "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
        "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
        "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
        "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
        "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
    }

    def to_abbrev(s: str) -> str:
        s = s.strip().lower()
        if len(s) == 2:
            return s.upper()
        return STATE_ABBREV.get(s, s.upper())

    reasons = []
    geo_score = 0.0

    q_state = to_abbrev(query_state) if query_state else ""
    c_state = to_abbrev(candidate.get("state", ""))

    if q_state and c_state:
        if q_state == c_state:
            geo_score += 0.6
            reasons.append(f"state match ({c_state})")
        else:
            reasons.append(f"state mismatch ({q_state} vs {c_state})")

    q_city = query_city.strip().lower() if query_city else ""
    c_city = candidate.get("city", "").strip().lower()

    if q_city and c_city:
        city_sim = fuzz.token_set_ratio(q_city, c_city) / 100.0
        if city_sim >= 0.85:
            geo_score += 0.4
            reasons.append(f"city match ({candidate.get('city','')})")
        elif city_sim >= 0.6:
            geo_score += 0.2
            reasons.append(f"city partial match ({candidate.get('city','')})")

    return round(geo_score, 4), reasons


def reconcile(
    query_name: str,
    query_city: str = "",
    query_state: str = "",
    query_cert: str = "",
    query_charter: str = "",
    query_rssd: str = "",
    institutions: list[dict] = None,
    top_n: int = 5,
) -> list[dict]:
    """
    Match a messy external record against the institution snapshot.
    Returns ranked candidates with confidence scores and match reasons.
    """
    if not institutions:
        return []

    query_norm = normalize_name(query_name)
    results = []

    for inst in institutions:
        reasons = []
        confidence = 0.0

        # --- Exact identifier override ---
        exact_id_hit = False
        if query_cert and inst.get("cert") == query_cert and inst["source"] == "fdic":
            confidence = 1.0
            reasons.append(f"exact FDIC cert match ({query_cert})")
            exact_id_hit = True
        if query_charter and inst.get("charter_number") == query_charter and inst["source"] == "ncua":
            confidence = 1.0
            reasons.append(f"exact NCUA charter match ({query_charter})")
            exact_id_hit = True
        if query_rssd and inst.get("rssdid") == query_rssd and inst.get("rssdid") not in ("", "0"):
            confidence = 1.0
            reasons.append(f"exact RSSD match ({query_rssd})")
            exact_id_hit = True

        if exact_id_hit:
            results.append({
                "confidence": confidence,
                "reasons": reasons,
                "inst": inst,
            })
            continue

        # --- Name scoring (weight: 0.6) ---
        cand_norm = normalize_name(inst.get("name", ""))
        name_score = score_name(query_norm, cand_norm)

        # Skip poor name matches early for speed
        if name_score < 0.35:
            continue

        confidence += 0.6 * name_score
        if name_score >= 0.9:
            reasons.append(f"strong name match ('{inst.get('name','')}')")
        elif name_score >= 0.7:
            reasons.append(f"good name match ('{inst.get('name','')}')")
        else:
            reasons.append(f"weak name match ('{inst.get('name','')}')")

        # --- Geographic scoring (weight: 0.4) ---
        geo_score, geo_reasons = score_geo(query_city, query_state, inst)
        confidence += 0.4 * geo_score
        reasons.extend(geo_reasons)

        results.append({
            "confidence": round(confidence, 3),
            "reasons": reasons,
            "inst": inst,
        })

    # Sort by confidence descending
    results.sort(key=lambda x: x["confidence"], reverse=True)

    # Format output
    output = []
    for r in results[:top_n]:
        inst = r["inst"]
        entry = {
            "confidence": r["confidence"],
            "match_reasons": r["reasons"],
            "name": inst.get("name", ""),
            "type": "Credit Union" if inst["source"] == "ncua" else "Bank / Thrift",
            "city": inst.get("city", ""),
            "state": inst.get("state", ""),
            "source": inst["source"],
        }
        if inst["source"] == "fdic":
            entry["fdic_cert"] = inst.get("cert", "")
            entry["rssdid"] = inst.get("rssdid", "")
        else:
            entry["ncua_charter"] = inst.get("charter_number", "")
            entry["rssdid"] = inst.get("rssdid", "")
        output.append(entry)

    return output
