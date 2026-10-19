"""
core/company_registry.py, Single source of truth mapping insurance-company
IDs (stable, used by the client application / database) to the internal display
name (used throughout policy_engine.py for prompt selection, sheet routing,
etc).

Why IDs at all: the client application stores everything against the numeric ID,
not the name. If a company's display name ever changes, only the mapping
below needs to be updated, no historical data (DB rows, saved Excel sheets,
logs) needs to be touched or migrated.

To add / rename a company, edit COMPANY_ID_MAP only. Nothing else in the
codebase should hardcode an ID.
"""

from typing import Optional, Tuple

# id -> internal display name (this exact string is what policy_engine.py
# and determine_policy_sheet_name() compare against everywhere)
COMPANY_ID_MAP = {
    1: "ZUNO",
    2: "HDFC ERGO",
    3: "TATA AIG",
    4: "GO DIGIT",
    5: "ICICI Lombard",
}

# Reverse lookup, built once. Useful if some caller only has the name and
# needs the ID back (e.g. for echoing both in a response).
COMPANY_NAME_TO_ID = {name: cid for cid, name in COMPANY_ID_MAP.items()}

# Accepted spellings/aliases that should still resolve to a valid name,
# kept only as a soft-deprecated fallback for callers not yet migrated to
# sending the ID. New integrations should send the ID exclusively.
_NAME_ALIASES = {
    "ZUNO": "ZUNO",
    "EDELWEISS": "ZUNO",
    "HDFC": "HDFC ERGO",
    "HDFC ERGO": "HDFC ERGO",
    "HDFCERGO": "HDFC ERGO",
    "HDFC-ERGO": "HDFC ERGO",
    "TATA": "TATA AIG",
    "TATA AIG": "TATA AIG",
    "TATAAIG": "TATA AIG",
    "TATA-AIG": "TATA AIG",
    "GO DIGIT": "GO DIGIT",
    "GODIGIT": "GO DIGIT",
    "GO-DIGIT": "GO DIGIT",
    "GO_DIGIT": "GO DIGIT",
    "ICICI": "ICICI Lombard",
    "ICICI LOMBARD": "ICICI Lombard",
    "ICICILOMBARD": "ICICI Lombard",
    "ICICI-LOMBARD": "ICICI Lombard",
    "ICLB": "ICICI Lombard",
}


class InvalidCompanyError(ValueError):
    """Raised when a company id/name can't be resolved."""


def resolve_company(raw_value: str) -> Tuple[int, str]:
    """
    Resolve a raw 'company' field value (as received over the API) to a
    (company_id, company_name) pair.

    Accepts:
      - A numeric ID as a string, e.g. "1" -> (1, "ZUNO")   [preferred]
      - A legacy name string, e.g. "ZUNO" / "HDFC" -> resolved via
        _NAME_ALIASES, ID looked up from COMPANY_NAME_TO_ID [deprecated
        fallback, logged by the caller]

    Raises InvalidCompanyError if the value can't be resolved to a known
    company.
    """
    raw = (raw_value or "").strip()
    if not raw:
        raise InvalidCompanyError("empty company value")

    if raw.isdigit():
        company_id = int(raw)
        name = COMPANY_ID_MAP.get(company_id)
        if name is None:
            raise InvalidCompanyError(
                f"Unknown company id {company_id!r}. "
                f"Known ids: {sorted(COMPANY_ID_MAP)}"
            )
        return company_id, name

    normalized = raw.upper().replace("_", " ").strip()
    name = _NAME_ALIASES.get(normalized) or _NAME_ALIASES.get(raw.upper())
    if name is None:
        raise InvalidCompanyError(
            f"Unknown company value {raw_value!r}. "
            f"Send a numeric company id. Known ids: {COMPANY_ID_MAP}"
        )
    return COMPANY_NAME_TO_ID[name], name


def company_id_for_name(name: str) -> Optional[int]:
    """Best-effort ID lookup for a resolved internal display name."""
    return COMPANY_NAME_TO_ID.get(name)
