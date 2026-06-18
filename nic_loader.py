"""
nic_loader.py — Parses FFIEC NIC bulk ZIPs for TRANSFORMATIONS, RELATIONSHIPS,
and institution name lookups (both active and closed attributes).
"""

import zipfile
import csv
import io
import os
import glob
import logging

logger = logging.getLogger(__name__)


def find_zip(cache_dir: str, filename: str) -> str | None:
    """Find a specific ZIP in the cache directory by exact filename."""
    path = os.path.join(str(cache_dir), filename)
    return path if os.path.exists(path) else None


def read_csv_from_zip(zip_path: str, filename: str) -> list[dict]:
    """
    Open a ZIP and read a specific CSV from inside it.
    Returns list of dicts or empty list if not found.
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            match = next(
                (n for n in names if os.path.basename(n).upper() == filename.upper()),
                None,
            )
            if not match:
                logger.warning(f"  {filename} not found inside {os.path.basename(zip_path)}")
                logger.warning(f"  Files in ZIP: {names}")
                return []

            with zf.open(match) as f:
                content = f.read().decode("latin-1")
                reader = csv.DictReader(io.StringIO(content))
                rows = list(reader)
                logger.info(f"  Read {len(rows):,} rows from {filename}")
                return rows
    except Exception as e:
        logger.error(f"  Error reading {filename} from ZIP: {e}")
        return []


def load_nic_names(cache_dir: str) -> dict:
    """
    Build a dict mapping RSSD ID (str) -> name info by reading both
    CSV_ATTRIBUTES_ACTIVE.zip and CSV_ATTRIBUTES_CLOSED.zip.
    Returns {} if neither file is found.
    """
    cache_dir = str(cache_dir)
    name_lookup = {}

    sources = [
        ("CSV_ATTRIBUTES_ACTIVE.zip",  "CSV_ATTRIBUTES_ACTIVE.CSV"),
        ("CSV_ATTRIBUTES_CLOSED.zip",  "CSV_ATTRIBUTES_CLOSED.CSV"),
    ]

    for zip_filename, csv_filename in sources:
        zip_path = find_zip(cache_dir, zip_filename)
        if not zip_path:
            logger.warning(f"  {zip_filename} not found in cache/ — skipping.")
            continue

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                csv_name = next(
                    (n for n in zf.namelist() if n.upper().endswith(".CSV")),
                    None,
                )
                if not csv_name:
                    logger.warning(f"  No CSV found inside {zip_filename}")
                    continue

                text = zf.read(csv_name).decode("utf-8", errors="replace")

            lines = text.splitlines()
            if lines and lines[0].startswith("#"):
                lines[0] = lines[0][1:]

            added = 0
            for row in csv.DictReader(lines):
                rssd = row.get("ID_RSSD", "").strip()
                if not rssd or rssd == "0":
                    continue
                name = (
                    row.get("NM_SHORT", "").strip()
                    or row.get("NM_LGL", "").strip()
                )
                city  = row.get("CITY", "").strip()
                state = row.get("STATE_ABBR_NM", "").strip()
                if name and rssd not in name_lookup:
                    name_lookup[rssd] = {
                        "name":  name,
                        "city":  city,
                        "state": state,
                    }
                    added += 1

            logger.info(f"  NIC name lookup: added {added:,} names from {zip_filename}")

        except Exception as e:
            logger.error(f"  Error reading {zip_filename}: {e}")

    logger.info(f"  NIC name lookup total: {len(name_lookup):,} institutions")
    return name_lookup


def parse_transformations(rows: list[dict]) -> dict:
    """
    Parse CSV_TRANSFORMATIONS.CSV rows into a dict keyed by RSSD ID.

    Columns:
      #ID_RSSD_PREDECESSOR  — predecessor institution
      ID_RSSD_SUCCESSOR     — surviving/successor institution
      TRNSFM_CD             — numeric transformation type code
      DT_TRANS              — date of transformation (YYYYMMDD)
    """
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

    result: dict[str, dict] = {}

    def _add(rssd_id: str, direction: str, record: dict):
        if not rssd_id or rssd_id.strip() == "":
            return
        rssd_id = rssd_id.strip()
        if rssd_id not in result:
            result[rssd_id] = {"as_predecessor": [], "as_successor": []}
        result[rssd_id][direction].append(record)

    for row in rows:
        pred_rssd = row.get("#ID_RSSD_PREDECESSOR", "").strip()
        succ_rssd = row.get("ID_RSSD_SUCCESSOR", "").strip()
        code      = row.get("TRNSFM_CD", "").strip()
        date_raw  = row.get("DT_TRANS", "").strip()

        if len(date_raw) == 8 and date_raw.isdigit():
            date_fmt = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}"
        else:
            date_fmt = date_raw

        record = {
            "predecessor_rssd":         pred_rssd,
            "successor_rssd":           succ_rssd,
            "transformation_type_code": code,
            "transformation_type":      TRNSFM_LABELS.get(code, f"Type {code}"),
            "transformation_date":      date_fmt,
        }

        _add(pred_rssd, "as_predecessor", record)
        _add(succ_rssd, "as_successor", record)

    logger.info(f"  Transformations index covers {len(result):,} unique RSSD IDs")
    return result


def parse_relationships(rows: list[dict]) -> dict:
    """
    Parse CSV_RELATIONSHIPS.CSV rows into a dict keyed by RSSD ID.

    Columns:
      #ID_RSSD_PARENT   — parent institution RSSD ID
      ID_RSSD_OFFSPRING — child/subsidiary RSSD ID
      DT_END            — end date (99991231 or blank = still active)
      RELN_LVL          — relationship level (1 = direct parent)
      PCT_EQUITY        — equity ownership percentage
    """
    result: dict[str, dict] = {}

    def _ensure(rssd_id: str):
        if rssd_id not in result:
            result[rssd_id] = {"parent_rssd": None, "subsidiaries": [], "equity_pct": None}

    for row in rows:
        parent_rssd = row.get("#ID_RSSD_PARENT", "").strip()
        child_rssd  = row.get("ID_RSSD_OFFSPRING", "").strip()
        dt_end      = row.get("DT_END", "").strip()
        pct_equity  = row.get("PCT_EQUITY", "").strip()
        reln_lvl    = row.get("RELN_LVL", "").strip()

        if not parent_rssd or not child_rssd:
            continue

        if dt_end and dt_end not in ("", "0", "99991231"):
            continue

        _ensure(parent_rssd)
        _ensure(child_rssd)

        if reln_lvl == "1" or not reln_lvl:
            result[child_rssd]["parent_rssd"] = parent_rssd
            result[child_rssd]["equity_pct"]  = pct_equity if pct_equity else None

        if child_rssd not in result[parent_rssd]["subsidiaries"]:
            result[parent_rssd]["subsidiaries"].append(child_rssd)

    logger.info(f"  Relationships index covers {len(result):,} unique RSSD IDs")
    return result


def load_nic_data(cache_dir: str) -> tuple[dict, dict, dict]:
    """
    Main entry point called by data_loader.py.
    Returns (transformations_dict, relationships_dict, nic_names_dict).
    All three dicts keyed by RSSD ID string.
    """
    cache_dir = str(cache_dir)

    logger.info("[NIC] Loading institution name lookup (active + closed)...")
    nic_names = load_nic_names(cache_dir)

    trans_zip = find_zip(cache_dir, "CSV_TRANSFORMATIONS.zip")
    transformations = {}
    if trans_zip:
        logger.info("  NIC Transformations ZIP found: CSV_TRANSFORMATIONS.zip")
        trans_rows = read_csv_from_zip(trans_zip, "CSV_TRANSFORMATIONS.CSV")
        transformations = parse_transformations(trans_rows) if trans_rows else {}
    else:
        logger.warning("  CSV_TRANSFORMATIONS.zip not found — skipping transformation history.")

    rel_zip = find_zip(cache_dir, "CSV_RELATIONSHIPS.zip")
    relationships = {}
    if rel_zip:
        logger.info("  NIC Relationships ZIP found: CSV_RELATIONSHIPS.zip")
        rel_rows = read_csv_from_zip(rel_zip, "CSV_RELATIONSHIPS.CSV")
        relationships = parse_relationships(rel_rows) if rel_rows else {}
    else:
        logger.warning("  CSV_RELATIONSHIPS.zip not found — skipping relationship data.")

    return transformations, relationships, nic_names
