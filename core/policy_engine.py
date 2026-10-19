"""
core/policy_engine.py: High-performance Insurance GI Policy Extraction Engine.
Complete extraction rules, insurer-specific heuristics (Zuno, HDFC ERGO, TATA AIG, Go Digit,
ICICI Lombard, Star Health, Niva Bupa), prompt builders, JSON schemas,
fallback parsers, backfill algorithms, normalization, and validation.
"""
from __future__ import annotations

import json
import logging
import os
import re
import textwrap
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import urllib3
import requests

import config
try:
    import pymupdf as fitz  # PyMuPDF
except ImportError:
    import fitz

logger = logging.getLogger(__name__)

# SSL verification (defaults to False to match DailyTasksV2 and corporate inspection proxies)
SSL_VERIFY: bool = os.getenv("SSL_VERIFY", "false").lower() in ("true", "1", "yes")
if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from core.excel_utils import (
    DEMO_COLUMNS_WORKBOOK,
    DEFAULT_POLICY_OUTPUT as DEFAULT_OUTPUT_EXCEL,
    TEMPLATE_POLICY_COLUMNS,
    NUMERIC_FIELDS,
    DATE_TEMPLATE_COLUMNS,
    _append_row_to_excel,
    _ensure_policy_template_copy,
    format_excel_file,
    coerce_excel_date_value,
)



def _safe_log(log_placeholder, msg: Any, *args: Any, **kwargs: Any) -> None:
    if not log_placeholder:
        return
    text = "\n".join(msg) if isinstance(msg, (list, tuple)) else str(msg)
    clean_text = re.sub(r"<[^>]+>", "", text).strip()
    try:
        if callable(log_placeholder):
            log_placeholder(clean_text or text)
        elif hasattr(log_placeholder, "markdown"):
            log_placeholder.markdown(f'<div class="progress-log">{clean_text or text}</div>', unsafe_allow_html=True)
        elif hasattr(log_placeholder, "caption"):
            log_placeholder.caption(clean_text or text)
        elif hasattr(log_placeholder, "info"):
            log_placeholder.info(clean_text or text)
    except Exception:
        pass

def detect_insurer_from_text(raw_text: str) -> str:
    """
    Auto-detect the insurance company from extracted policy text layer.
    Returns: 'HDFC ERGO', 'TATA AIG', 'GO DIGIT', 'ZUNO', 'ICICI Lombard', 'Bajaj Allianz',
             'Star Health', 'Care Health', 'Niva Bupa', 'National Insurance',
             'New India Assurance', 'Oriental Insurance', 'United India', or 'ZUNO' as fallback.
    """
    if not raw_text:
        return "ZUNO"
    t = str(raw_text[:4000]).upper()
    if "HDFC ERGO" in t or "HDFC GENERAL" in t or "HDFCERGO" in t:
        return "HDFC ERGO"
    if "TATA AIG" in t or "TATAAIG" in t or "TATA-AIG" in t or "WITH YOU ALWAYS" in t:
        return "TATA AIG"
    if "GO DIGIT" in t or "GODIGIT" in t or "DIGIT GENERAL INSURANCE" in t:
        return "GO DIGIT"
    if "ZUNO" in t or "EDELWEISS" in t or "ZUNOGENERAL" in t:
        return "ZUNO"
    if "ICICI LOMBARD" in t or "ICICILOMBARD" in t or "ICICI BHARAT" in t:
        return "ICICI Lombard"
    if "BAJAJ ALLIANZ" in t or "BAJAJALLIANZ" in t:
        return "Bajaj Allianz"
    if "STAR HEALTH" in t or "STARHEALTH" in t:
        return "Star Health"
    if "CARE HEALTH" in t or "RELIGARE" in t:
        return "Care Health"
    if "NIVA BUPA" in t or "MAX BUPA" in t or "NIVABUPA" in t:
        return "Niva Bupa"
    if "NATIONAL INSURANCE" in t:
        return "National Insurance"
    if "NEW INDIA ASSURANCE" in t:
        return "New India Assurance"
    if "ORIENTAL INSURANCE" in t:
        return "Oriental Insurance"
    if "UNITED INDIA" in t:
        return "United India"
    return "ZUNO"


PROVIDERS = {
    "AzureAI": {
        "base_url": config.AZURE_AI_ENDPOINT,
        "model": config.AZURE_AI_MODEL,
        "key_env": "AZURE_AI_API_KEY",
        "key_help": "Azure AI Foundry endpoint and API key.",
        "note": "Using the default Azure AI Foundry project configuration.",
    },
    "Google Gemini (Free)": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-1.5-flash",
        "key_env": "GEMINI_API_KEY",
        "key_help": "Get a free key at https://aistudio.google.com (no credit card needed).",
        "note": "Free tier: 1,500 requests/day, 15 req/min. More than enough for policy reading.",
    },
    "Groq (Free, very fast)": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama3-8b-8192",
        "key_env": "GROQ_API_KEY",
        "key_help": "Get a free key at https://console.groq.com",
        "note": "Free tier: ~14,400 requests/day. Llama 3 is a capable extraction model.",
    },
    "OpenAI": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "key_env": "OPENAI_API_KEY",
        "key_help": "Get a key at https://platform.openai.com",
        "note": "gpt-4o-mini is cheap (~$0.0001/policy) and very accurate.",
    },
    "Azure OpenAI": {
        "base_url": "",
        "model": "",
        "key_env": "AZURE_OPENAI_API_KEY",
        "key_help": "Use the API key from your Azure OpenAI resource.",
        "note": "Paste your 'Project endpoint' and 'Deployment name' from Azure AI Foundry below.",
    },
    "Custom / Other": {
        "base_url": "",
        "model": "",
        "key_env": "OPENAI_API_KEY",
        "key_help": "Enter any OpenAI-compatible endpoint base URL and model name.",
        "note": "",
    },
}

DEFAULT_AZURE_API_VERSION = "2024-10-21"
DEFAULT_FOUNDRY_API_VERSION = "2025-04-01-preview"


POLICY_FIELDS_FILE_CANDIDATES = [
    DEMO_COLUMNS_WORKBOOK,
    Path(__file__).parent / "policy_fields.xlsx",
]

DEFAULT_POLICY_FIELDS = [
    ("customer_name",       "Customer Name"),
    ("address",             "Address"),
    ("city",                "City"),
    ("state",               "State"),
    ("pin",                 "PIN / Postal Code"),
    ("phone_number",        "Phone Number"),
    ("mobile_number",       "Mobile Number"),
    ("dob",                 "Date of Birth"),
    ("email",               "Email"),
    ("insurer",             "Insurer"),
    ("business_type",       "Business Type"),
    ("policy_type",         "Policy Type"),
    ("vehicle_number",      "Vehicle Number"),
    ("make",                "Make"),
    ("model_variant",       "Model & Variant"),
    ("year_of_manufacture", "Year of Manufacture"),
    ("chassis_number",      "Chassis Number"),
    ("engine_number",       "Engine Number"),
    ("cc_kwh",              "CC / KWH"),
    ("fuel_type",           "Fuel Type"),
    ("rto_name",            "RTO Name"),
    ("current_ncb",         "Current NCB %"),
    ("previous_ncb",        "Previous NCB %"),
    ("passenger",           "Passenger"),
    ("gvw",                 "GVW"),
    ("policy_issue_date",   "Policy Issue Date"),
    ("policy_start",        "Policy Start Date"),
    ("policy_end",          "Policy End Date"),
    ("txn_date",            "Txn Date"),
    ("policy_number",       "Policy Number"),
    ("idv",                 "IDV"),
    ("od_net_premium",      "OD Net Premium"),
    ("tp_net_premium",      "TP Net Premium"),
    ("gst",                 "GST"),
    ("cpa",                 "CPA"),
    ("payment_mode",        "Payment Mode"),
    ("cheque_number",       "Cheque Number"),
    ("bank_name",           "Bank Name"),
    ("payment_amount",      "Payment Amount"),
]

_DEFAULT_POLICY_FIELD_LABELS = {key: label for key, label in DEFAULT_POLICY_FIELDS}

_POLICY_FIELD_KEY_ALIASES = {
    "sno": "sno",
    "batchid": "batch_id",
    "filename": "file_name",
    "customername": "customer_name",
    "customer_name": "customer_name",
    "address": "address",
    "city": "city",
    "state": "state",
    "pin": "pin",
    "phoneno": "phone_number",
    "mobileno": "mobile_number",
    "dob": "dob",
    "email": "email",
    "insurer": "insurer",
    "businesstype": "business_type",
    "business_type": "business_type",
    "businesstype_": "business_type",
    "policytype": "policy_type",
    "vehicleno": "vehicle_number",
    "make": "make",
    "modelvariant": "model_variant",
    "modelandvariant": "model_variant",
    "model_variant": "model_variant",
    "make_model": "make_model",
    "makeandmodel": "make_model",
    "makemodel": "make_model",
    "variant": "variant",
    "year_of_manufacturing": "year_of_manufacture",
    "yearofmanufacturing": "year_of_manufacture",
    "chassisno": "chassis_number",
    "engineno": "engine_number",
    "cckwh": "cc_kwh",
    "fuel": "fuel_type",
    "rtoname": "rto_name",
    "currentncb": "current_ncb",
    "currentncb_": "current_ncb",
    "previousncb": "previous_ncb",
    "previousncb_": "previous_ncb",
    "passenger": "passenger",
    "gvw": "gvw",
    "policyissuedate": "policy_issue_date",
    "policystartdate": "policy_start",
    "policyenddate": "policy_end",
    "txndate": "txn_date",
    "policyno": "policy_number",
    "idv": "idv",
    "odnetpremium": "od_net_premium",
    "tpnetpremium": "tp_net_premium",
    "gst": "gst",
    "grosspremium": "gross_premium",
    "netpremium": "net_premium",
    "opsremarks": "ops_remarks",
    "cpa": "cpa",
    "paownerdriver": "cpa",
    "pa_owner_driver": "cpa",
    "paymentmode": "payment_mode",
    "payment_mode": "payment_mode",
    "chequenumber": "cheque_number",
    "cheque_number": "cheque_number",
    "chequeno": "cheque_number",
    "instrumentnumber": "cheque_number",
    "instrument_reference_number": "cheque_number",
    "bankname": "bank_name",
    "bank_name": "bank_name",
    "paymentamount": "payment_amount",
    "payment_amount": "payment_amount",
    "premiumpaid": "payment_amount",
    "finalpremium": "payment_amount",
}

_NON_EXTRACTION_KEYS = {
    "sno",
    "batch_id",
    "file_name",
    "ops_remarks",
    "refer_code",
    "refercode",
    "financier",
    "financername",
    "financer_name",
}


def _normalize_field_key(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "", str(value or "").strip().lower())


def _canonical_field_key(raw_name: str) -> str:
    normalized = _normalize_field_key(raw_name)
    return _POLICY_FIELD_KEY_ALIASES.get(normalized, raw_name.strip().lower().replace(" ", "_"))


def _friendly_label(column_name: str) -> str:
    parts = re.split(r"[_\s]+", str(column_name).strip())
    return " ".join(word.capitalize() for word in parts if word)


def _load_policy_fields_from_excel(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    try:
        import pandas as pd
    except ImportError:
        return []

    try:
        df = pd.read_excel(path, nrows=0)
    except Exception:
        return []

    fields: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for col in df.columns:
        raw_name = str(col).strip()
        if not raw_name:
            continue
        key = _canonical_field_key(raw_name)
        if key in _NON_EXTRACTION_KEYS or key in {"file", "filename"}:
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        fields.append((key, _DEFAULT_POLICY_FIELD_LABELS.get(key, _friendly_label(key))))
    return fields


def _ensure_extra_fields(fields: list[tuple[str, str]]) -> list[tuple[str, str]]:
    required_fields = [
        ("address", "Address"),
        ("city", "City"),
        ("state", "State"),
    ]
    extras = [
        ("needs_review",  "Needs Manual Review?"),
        ("review_reason", "Reason for Review"),
    ]
    existing = {key for key, _ in fields}
    for key, label in required_fields:
        if key not in existing:
            fields.append((key, label))
            existing.add(key)
    for key, label in extras:
        if key not in existing:
            fields.append((key, label))
    return fields


def load_policy_fields() -> list[tuple[str, str]]:
    for candidate in POLICY_FIELDS_FILE_CANDIDATES:
        policy_fields = _load_policy_fields_from_excel(candidate)
        if policy_fields:
            return _ensure_extra_fields(policy_fields)
    return _ensure_extra_fields(list(DEFAULT_POLICY_FIELDS))




def _initialise() -> list[tuple[str, str]]:
    """Load fields and prepare the output workbook.

    Deferred from module level so that importing this module in tests or
    other contexts does not immediately attempt file I/O, which can raise
    PermissionError, FileNotFoundError, or OSError.
    """
    fields = load_policy_fields()
    try:
        _ensure_policy_template_copy(DEFAULT_OUTPUT_EXCEL)
    except (PermissionError, FileNotFoundError, OSError):
        pass
    return fields


POLICY_FIELDS = _initialise()


EXTRACTION_PROMPT_TEMPLATE = textwrap.dedent('''\
You are an insurance document parser. Extract ONLY information that is
explicitly present in the policy text below; never guess, infer, or carry
over an example value. If a field is not stated in the text, or the
document type makes it not applicable (e.g. vehicle fields on a life or
health policy), its value MUST be null. Respond ONLY with a valid JSON
object: no preamble, no markdown fences, no extra keys, no commentary.

Rules:
- Copy values as they appear in the text (numbers, dates, names) rather
  than paraphrasing or reformatting them, except where a field's
  description below asks for a specific format.
- For date fields (policy_issue_date, policy_start, policy_end, txn_date),
    return ONLY the calendar date in dd/mm/yyyy format.
    Never include a time component such as 00:00:00, 12:15 PM, timestamps,
    timezone text, or full datetime strings.
- Never fill a field with a fragment of unrelated boilerplate text just
  because it appears near a keyword. Only use text that actually answers
  that field.
- If two different values could both plausibly answer a field, prefer the
  one stated closest to the field's own label in the document.
- For name fields (customer_name, insurer), extract ONLY the person's or
  entity's name. If a customer ID, mobile number, member number, or any
  other code appears immediately next to the name (e.g. joined by a
  slash, dash, or on the same line), leave it out of the name field
  entirely; do not concatenate it onto the name.
{company_rules}

Required JSON keys (use null if a field cannot be found or does not apply):
{fields}

Policy text:
"""
{policy_text}
"""

JSON:''')


DATE_ONLY_FIELDS = {
    "dob",
    "policy_issue_date",
    "policy_start",
    "policy_end",
    "txn_date",
}

MIN_VALID_DOB_YEAR = 1950
NOMINEE_BOUNDARY_RE = re.compile(
    r"\b(?:nominee\s+details|who(?:'s|\s+is)\s+your\s+nominee)\b",
    flags=re.IGNORECASE,
)

_ZUNO_POLICY_RE = re.compile(
    r"\b(?:zuno\s+smartdrive|zuno\s+general\s+insurance)\b",
    flags=re.IGNORECASE,
)

_HDFC_TEL_RE = re.compile(r"(?:Tel|Ph)[.:]?\s*([0-9][0-9Xx*]{9,14})", re.IGNORECASE)

_HDFC_POLICY_RE = re.compile(
    r"\b(?:hdfc\s+ergo|hdfc\s+general\s+insurance)\b",
    flags=re.IGNORECASE,
)

_TATA_POLICY_RE = re.compile(
    r"\b(?:tata\s+aig|tata\s+aig\s+general\s+insurance)\b",
    flags=re.IGNORECASE,
)

_GO_DIGIT_POLICY_RE = re.compile(
    r"\b(?:go\s*digit|gdgil|digit\s+general\s+insurance|digit\s+private\s+car)\b",
    flags=re.IGNORECASE,
)

_ICICI_POLICY_RE = re.compile(
    r"\b(?:icici\s*lombard|icicilombard|icici\s*bharat)\b",
    flags=re.IGNORECASE,
)

# Go Digit watermark noise: isolated single tokens (numbers/letters/fragments)
# scattered by the PDF overlay renderer, e.g. "N\n01\nG0\nP\n1M\n1\n61\n4.8\nMA\nfal"
# These appear as runs of very short tokens (1-6 chars) with no real words,
# right after the insurer boilerplate footer block.
_GO_DIGIT_WATERMARK_RE = re.compile(
    r"(?:(?:^|\n)[A-Za-z0-9.]{1,6}){4,}",
    flags=re.MULTILINE,
)
_GODIGIT_WATERMARK_FOOTER_RE = re.compile(
    r"[A-Z][A-Z ]{3,}\s+[A-Z]{2}\d{2}[A-Z]+\d+\s+[A-Z ]{4,}\s+\d{4}-\d{2}-\d{2}"
)
_REAL_WORD_RE = re.compile(r"[A-Za-z]{4,}|[A-Za-z]*[aeiouAEIOU][a-zA-Z]{2,}")


_ZUNO_PROPOSER_FIELD_KEYS = {
    "customer_name",
    "address",
    "city",
    "state",
    "pin",
    "phone_number",
    "mobile_number",
    "dob",
    "email",
}

_GO_DIGIT_PAGE_1_FIELDS = {
    "customer_name", "address", "city", "state", "pin", "phone_number",
    "mobile_number", "dob", "email", "vehicle_number", "make", "model_variant",
    "year_of_manufacture", "chassis_number", "engine_number", "cc_kwh", "fuel_type",
    "rto_name", "passenger", "gvw",
}

_GO_DIGIT_MIDDLE_PAGE_FIELDS = {
    "policy_type", "policy_start", "policy_end", "tp_start", "tp_end", "idv",
    "od_net_premium", "tp_net_premium", "net_premium", "gross_premium", "gst",
    "current_ncb", "previous_ncb", "cpa", "payment_mode", "cheque_number",
    "bank_name", "payment_amount", "business_type",
}

_GO_DIGIT_FINAL_PAGE_FIELDS = {"policy_issue_date", "txn_date"}


def _is_zuno_policy_document(text: str) -> bool:
    return bool(_ZUNO_POLICY_RE.search(str(text or "")))


def _is_hdfc_policy_document(text: str) -> bool:
    return bool(_HDFC_POLICY_RE.search(str(text or "")))


def _is_tata_policy_document(text: str) -> bool:
    return bool(_TATA_POLICY_RE.search(str(text or "")))


def _is_go_digit_policy_document(text: str) -> bool:
    return bool(_GO_DIGIT_POLICY_RE.search(str(text or "")))


def _is_icici_policy_document(text: str) -> bool:
    return bool(_ICICI_POLICY_RE.search(str(text or "")))


# Keywords that identify HDFC ERGO as the previous/current insurer.
# Used to distinguish Renewal (previous insurer == HDFC ERGO) from
# Rollover (previous insurer is a different company).
_HDFC_ERGO_INSURER_KEYWORDS = re.compile(
    r"\bhdfc\s+ergo\b|\bhdfc\s+general\s+insurance\b",
    flags=re.IGNORECASE,
)

# Common insurer name fragments that, when found in the previous-insurer field,
# confirm the previous policy was NOT with HDFC ERGO (i.e. Rollover).
_KNOWN_OTHER_INSURER_RE = re.compile(
    r"\b(?:bajaj\s+allianz|icici\s+lombard|tata\s+aig|new\s+india|national\s+insurance|"
    r"oriental\s+insurance|united\s+india|reliance\s+general|go\s*digit|acko|"
    r"future\s+generali|iffco[\s-]*tokio|sbi\s+general|royal\s+sundaram|"
    r"chola\s+(?:ms|mandalam)|kotak\s+general|navi\s+general|zuno|edelweiss|"
    r"universal\s+sompo|magma\s+hdi|shriram\s+general|liberty\s+general|"
    r"star\s+health|care\s+health|niacl|uiic)\b",
    flags=re.IGNORECASE,
)


def _determine_hdfc_business_type(text: str) -> str:
    """Determine HDFC ERGO policy business type from the raw PDF text.

    Logic:
    - New Business : No previous policy details are present in the document.
    - Renewal      : A previous policy number exists AND the previous insurer
                     is HDFC ERGO (same company).
    - Rollover     : A previous policy number exists AND the previous insurer
                     is a different company (not HDFC ERGO).

    The function first looks for the previous policy number, and if found,
    inspects the previous insurer field that HDFC ERGO prints in the schedule
    (typically labelled 'Previous Insurer', 'Previous Policy Insurer', or
    'Previous Policy Issued By').  If no insurer label is found the policy
    number itself is checked: HDFC ERGO policy numbers follow a well-known
    numeric format (starting with digits like '23…', '24…', '25…'), whereas
    other insurers tend to use letter-prefixed alphanumeric strings.
    """
    raw = str(text or "")

    # ── Step 1: Is there a previous policy number at all? ─────────────────
    prev_policy_m = re.search(
        r"Previous\s+Policy\s+(?:No\.?|Number)\s*[:#-]?\s*([A-Za-z0-9/\-]{5,})",
        raw,
        flags=re.IGNORECASE,
    )
    if not prev_policy_m:
        # No previous policy → New Business
        return "New Business"

    prev_policy_no = prev_policy_m.group(1).strip()

    # ── Step 2: Try to read the previous insurer label ────────────────────
    # HDFC ERGO prints lines such as:
    #   "Previous Insurer  HDFC ERGO General Insurance Company Limited"
    #   "Previous Policy Insurer  Bajaj Allianz General Insurance Co. Ltd"
    prev_insurer_m = re.search(
        r"Previous\s+(?:Policy\s+)?Insurer(?:\s+Name)?\s*[:#-]?\s*([^\n]{5,80})",
        raw,
        flags=re.IGNORECASE,
    )
    if prev_insurer_m:
        prev_insurer_text = prev_insurer_m.group(1).strip()
        if _HDFC_ERGO_INSURER_KEYWORDS.search(prev_insurer_text):
            return "Renewal"
        # Any other insurer name found → Rollover
        return "Rollover"

    # ── Step 3: No explicit insurer label, infer from previous policy no. ─
    # HDFC ERGO policy numbers are typically long all-numeric strings
    # (e.g. "1111222233334444000").  A purely numeric previous policy number
    # is a strong signal the previous policy was also with HDFC ERGO.
    if re.fullmatch(r"\d{10,}", prev_policy_no):
        return "Renewal"

    # Alphanumeric or letter-prefixed → likely another insurer → Rollover
    return "Rollover"


def _determine_tata_business_type(text: str) -> str:
    """Determine TATA AIG policy business type from the raw PDF text.

    Logic:
    - New Business : No 'Previous Insurance Details' section, or no previous
                     policy number present.
    - Renewal      : A previous policy number exists AND the previous insurer
                     is TATA AIG (same company).
    - Rollover     : A previous policy number exists AND the previous insurer
                     is a different company.

    TATA AIG prints previous insurer info in the 'Transcript Of Proposal'
    section under 'Previous Insurance Details':
        Name of the Insurer: GO DIGIT GENERAL INSURANCE CO LTD
        1. Policy Number: D214264770
    """
    raw = str(text or "")

    # Look for Previous Insurance Details section in the Transcript Of Proposal
    transcript_section_m = re.search(
        r"Previous\s+Insurance\s+Details\s*:(.*?)(?:Nominee\s+Details|Restriction\s+of\s+Cover|Declaration\s+for\s+No\s+Claim|$)",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not transcript_section_m:
        # No transcript section or no previous insurance details → New Business
        return "New Business"

    transcript_section = transcript_section_m.group(1).strip()

    # Check if there's an actual previous policy number
    prev_policy_m = re.search(
        r"Policy\s+Number\s*[:#-]?\s*([A-Za-z0-9/-]{3,})",
        transcript_section,
        flags=re.IGNORECASE,
    )
    if not prev_policy_m or not prev_policy_m.group(1).strip():
        return "New Business"

    # Read previous insurer name
    prev_insurer_m = re.search(
        r"Name\s+of\s+the\s+Insurer\s*[:#-]?\s*([^\n1-9]{3,100})",
        transcript_section,
        flags=re.IGNORECASE,
    )
    if prev_insurer_m:
        prev_insurer_text = prev_insurer_m.group(1).strip().upper()
        if any(k in prev_insurer_text for k in ["TATA", "TATAAIG", "TATA-AIG", "TATA AIG"]):
            return "Renewal"
        return "Rollover"

    if re.search(r"Name\s+of\s+the\s+Insurer\s*[:#-]?\s*[^\n]*TATA", transcript_section, re.IGNORECASE):
        return "Renewal"

    # Default to Rollover if previous policy exists but insurer name wasn't matched
    return "Rollover"


def _determine_go_digit_business_type(text: str) -> str:
    """Determine Go Digit policy business type from raw PDF text.

    Logic:
    - New Business : No previous policy details, or registration number is NEW.
    - Renewal      : Previous policy exists AND previous insurer is Go Digit.
    - Rollover     : Previous policy exists AND previous insurer is different insurer.
    """
    raw = str(text or "")

    # 1. Commercial "NEW" vehicles are always New Business
    if re.search(r"Vehicle\s+Registration\s+No\.?\s*\n\s*NEW\b", raw, re.IGNORECASE) or re.search(r"Registration\s+Number\s*:\s*\n\s*NEW\b", raw, re.IGNORECASE):
        return "New Business"

    # 2. Check for Previous Insurer / Policy
    prev_block = ""
    m = re.search(r"(?:Previous\s+Insurer|Previous\s+Policy\s+No|Previous\s+Own\s+Damage).{0,500}", raw, re.DOTALL | re.IGNORECASE)
    if m:
        prev_block = m.group(0)

    other_insurers = ['bajaj', 'icici', 'sompo', 'royal sundaram', 'hdfc', 'tata', 'reliance', 'oriental', 'national', 'new india', 'united', 'iffco', 'chola', 'sbi', 'kotak', 'magma', 'liberty', 'future', 'zuno']
    if any(ins in prev_block.lower() for ins in other_insurers):
        return "Rollover"
    elif 'digit' in prev_block.lower() or 'gdgil' in prev_block.lower():
        return "Renewal"
    elif re.search(r'\b(?:OG-\d|MOBL\d|\d{4}\s+\d{8})\b', prev_block):
        return "Rollover"
    elif re.search(r'Previous Own Damage', raw, re.IGNORECASE) or re.search(r'Previous Policy', raw, re.IGNORECASE):
        return "Renewal"

    return "New Business"


_ICICI_RENEWAL_KEYWORDS = re.compile(
    r"\b(?:ICICI\s*LOMBARD|ICICILOMBARD|ICLB)\b",
    flags=re.IGNORECASE,
)


def _determine_icici_business_type(text: str) -> str:
    """Determine ICICI Lombard policy business type from raw PDF text.

    Logic:
    - New Business : No 'Previous Policy Details' block present on page 1 / risk letter,
                     or registration number is literally 'NEW'.
    - Renewal      : 'Previous Policy Details' block present AND Previous Insurer
                     is ICICI itself (matches 'ICICI LOMBARD' or abbreviation 'ICLB').
    - Rollover     : 'Previous Policy Details' block present AND Previous Insurer
                     is a different company (confirmed samples include 'RSUND',
                     'BAJAJ ALIANZ', 'SBI', etc.).
    """
    raw = str(text or "")

    # 1. Commercial / new vehicle check
    if re.search(r"Vehicle\s+Registration\s+No\.?\s*[:\n]+\s*NEW\b", raw, re.IGNORECASE) or \
       re.search(r"Registration\s+Number\s*[:\n]+\s*NEW\b", raw, re.IGNORECASE):
        return "New Business"

    # 2. Check for Previous Policy Details section
    prev_block_m = re.search(
        r"Previous\s+Policy\s+Details\s*[:\n]+(.*?)(?:Third\s+Party\s+Insurance\s+Details|Tenure|Vehicle\s+Details|Cover\s+Details|The\s+commencement|The\s+information\s+provided|\n\n\n|$)",
        raw,
        re.DOTALL | re.IGNORECASE,
    )
    if not prev_block_m:
        return "New Business"

    prev_block = prev_block_m.group(1).strip()
    if not prev_block:
        return "New Business"

    if _ICICI_RENEWAL_KEYWORDS.search(prev_block):
        return "Renewal"

    if _KNOWN_OTHER_INSURER_RE.search(prev_block) or re.search(r"\b(?:RSUND|BAJAJ\s*ALIANZ|BAJAJ\s*ALLIANZ|SBI|ROYAL\s+SUNDARAM)\b", prev_block, re.IGNORECASE):
        return "Rollover"

    prev_ins_m = re.search(r"Previous\s+Insurer(?:\s+Name)?\s*[:#-]?\s*([^\n]{3,60})", prev_block, re.IGNORECASE)
    if prev_ins_m:
        val = prev_ins_m.group(1).strip().upper()
        if not any(hdr in val for hdr in ["PREVIOUS", "POLICY", "CLAIMS", "NCB"]):
            if "ICICI" in val or "ICLB" in val:
                return "Renewal"
            return "Rollover"

    pol_match = re.search(r"\b(300[158](?:/[A-Z])?/\d+|\d{8,}|[A-Za-z]{2,5}\d+)\b", prev_block)
    if pol_match:
        if pol_match.group(1).startswith("300"):
            return "Renewal"
        return "Rollover"

    return "Rollover"


def _extract_go_digit_fields(policy_text: str) -> dict:
    """Go Digit-specific deterministic field extractor.

    Handles all Go Digit motor policy types:
    - Digit Private Car Policy (Comprehensive: OD + TP)
    - Digit Private Car Stand-alone Own Damage Policy (SAOD)
    - Digit Private Car Liability Only Policy (TP only)
    - Digit Commercial Vehicle Comprehensive Policy / Insurance
    """
    raw = str(policy_text or "")

    # ── Insurer ────────────────────────────────────────────────────────────
    insurer = "Go Digit General Insurance Limited"

    # ── Policy Type ────────────────────────────────────────────────────────
    pt_m = re.search(
        r"Go Digit General Insurance Ltd\.\s*\n\s*(Digit [^\n]+(?:Policy|Cover|Insurance))",
        raw, re.IGNORECASE
    )
    policy_type = pt_m.group(1).strip() if pt_m else ""
    if not policy_type:
        wm_pt = re.search(r"\b(Digit Commercial Vehicle Comprehensive Policy)\b", raw, re.IGNORECASE)
        if wm_pt:
            policy_type = "Digit Commercial Vehicle Comprehensive Policy"
        elif re.search(r"Commercial Vehicle", raw, re.IGNORECASE):
            policy_type = "Digit Commercial Vehicle Comprehensive Policy"
        elif re.search(r"Stand-alone Own Damage", raw, re.IGNORECASE):
            policy_type = "Digit Private Car Stand-alone Own Damage Policy"
        elif re.search(r"Liability Only", raw, re.IGNORECASE):
            policy_type = "Digit Private Car Liability Only Policy"
        elif re.search(r"Digit Private Car Policy", raw, re.IGNORECASE):
            policy_type = "Digit Private Car Policy"

    is_tp_only = bool(re.search(r"Liability Only|TP Only", policy_type, re.IGNORECASE))
    is_saod = bool(re.search(r"Stand-alone Own Damage", policy_type, re.IGNORECASE))
    is_commercial = bool(re.search(r"Commercial Vehicle", policy_type, re.IGNORECASE))

    # ── Policy Number ──────────────────────────────────────────────────────
    # 1. Header D-number / DDMMYYYY (e.g. D123456789 / 20072026)
    pol_m = re.search(r"\b([A-Z][0-9]{7,10})\s*/\s*[0-9]{8}\b", raw)
    if not pol_m:
        pol_m = re.search(r"Policy\s*No\s*:\s*([A-Z0-9]+)", raw, re.IGNORECASE)
    if not pol_m:
        pol_m = re.search(r"(?<!Previous\s)(?<!Previous Own Damage\s)Policy\s*No[:.]?\s*([A-Z][0-9]{6,})", raw, re.IGNORECASE)
    if not pol_m:
        pol_m = re.search(r"\b(D\d{8,10})\b", raw)
    policy_number = pol_m.group(1).strip() if pol_m else ""

    # ── Customer Name ──────────────────────────────────────────────────────
    name_m = re.search(r"(?:^|\n)Name\s*\n([A-Z][A-Z .'-]{2,})\n", raw, re.MULTILINE)
    if not name_m:
        wm_m = re.search(r"^([A-Z][A-Z .'-]+)\s+(?:NEW|[A-Z]{2}[0-9][A-Z0-9]+)\s+[A-Z]", raw, re.MULTILINE)
        if wm_m:
            name_m = wm_m
        else:
            name_m = re.search(r"Name\s*:\s*\n\s*([A-Z][A-Z .'-]+)\n", raw)
    customer_name = name_m.group(1).strip() if name_m else ""
    customer_name = re.sub(
        r"\s+(?:Registration|Mobile|Email|Policy|Address|Aadhar|Partner|Vehicle|DL[0-9]).*",
        "", customer_name, flags=re.IGNORECASE
    ).strip()

    # ── Address, Pin, City, State ──────────────────────────────────────────
    pin_state_map = {
        '11': 'Delhi', '20': 'Uttar Pradesh', '22': 'Uttar Pradesh',
        '24': 'Uttar Pradesh', '25': 'Uttar Pradesh', '26': 'Uttar Pradesh',
        '27': 'Uttar Pradesh', '28': 'Uttar Pradesh', '14': 'Punjab',
        '40': 'Maharashtra', '41': 'Maharashtra', '42': 'Maharashtra',
        '43': 'Maharashtra', '44': 'Maharashtra', '12': 'Haryana',
        '13': 'Haryana', '16': 'Chandigarh', '30': 'Rajasthan',
        '31': 'Rajasthan', '32': 'Rajasthan', '33': 'Rajasthan',
        '34': 'Rajasthan', '56': 'Karnataka', '60': 'Tamil Nadu',
    }
    address = ''
    city = ''
    state = ''
    pin = ''
    addr_m = re.search(r'(?<!GSTIN\s)(?:^|\n)Address\s*\n(.*?)(?:\nPartner\s+Code|\nYOUR\s+VEHICLE|\nNominee|\nPartner\s+Email|\nInvoice\s+No|\nMobile)', raw, re.DOTALL | re.IGNORECASE)
    if addr_m:
        raw_addr = addr_m.group(1).replace('\n', ' ').strip()
        raw_addr = re.split(r'Optional\s+Cover|UIN|YOUR\s+VEHICLE', raw_addr, flags=re.IGNORECASE)[0].strip()
        raw_addr = re.sub(r'\s+', ' ', raw_addr)
        address = raw_addr
        
        parts = [p.strip() for p in raw_addr.split(',') if p.strip()]
        for p in parts:
            p_pin = re.search(r'\b(\d{6})\b', p)
            if p_pin:
                pin = p_pin.group(1)
                break
        if not pin:
            pins_all = re.findall(r'\b(\d{6})\b', raw_addr)
            if pins_all:
                pin = pins_all[0]
                
        c_m = re.findall(r',\s*([A-Za-z\s]+)-\s*\d{6}', raw_addr)
        if c_m:
            city = c_m[-1].strip()
    
    if not city:
        rto_m = re.search(r'RTO Location\s*\n\s*([A-Za-z\s]+)', raw)
        if rto_m:
            city = rto_m.group(1).strip()

    if pin:
        state = pin_state_map.get(pin[:2], '')
    if not state:
        if re.search(r'\b(?:Delhi|New Delhi)\b', address, re.I): state = 'Delhi'
        elif re.search(r'\b(?:Maharashtra|Mumbai|Pune|Thane|Vasai|Palghar)\b', address, re.I): state = 'Maharashtra'
        elif re.search(r'\b(?:Uttar Pradesh|UP|Noida|Kanpur|Barabanki|Lucknow)\b', address, re.I): state = 'Uttar Pradesh'
        elif re.search(r'\b(?:Punjab|Amritsar)\b', address, re.I): state = 'Punjab'

    if city:
        city = re.sub(r'[-–,]+$', '', city).strip()
        if 'DELHI' in city.upper():
            city = 'Delhi'
        elif 'NOIDA' in city.upper():
            city = 'Noida'
        elif 'KANPUR' in city.upper():
            city = 'Kanpur'
        elif 'BARABANKI' in city.upper():
            city = 'Barabanki'
        elif 'AMRITSAR' in city.upper():
            city = 'Amritsar'
        elif 'MUMBAI' in city.upper():
            city = 'Mumbai'
        elif 'PALGHAR' in city.upper() or 'VASAI' in city.upper():
            city = 'Palghar'

    # ── Mobile / Phone ─────────────────────────────────────────────────────
    mob_m = re.search(r"Mobile(?:\s+Number)?\s*[:\n]\s*([xX0-9]{7,15})", raw, re.IGNORECASE)
    if not mob_m:
        mob_m = re.search(r"\n([xX]{5,}[0-9]{4})\n", raw)
    mobile_number = mob_m.group(1).strip() if mob_m else ""
    phone_number = mobile_number

    # ── Email ──────────────────────────────────────────────────────────────
    email_m = re.search(r"(?:^|\n)Email\s*[:\n]\s*([a-zA-Z0-9.*_+%-]+@[a-zA-Z0-9._-]+)", raw, re.IGNORECASE | re.MULTILINE)
    email = ""
    if email_m:
        candidate = email_m.group(1).strip()
        domain = candidate.split("@")[-1].lower()
        if domain not in _BOILERPLATE_EMAIL_DOMAINS and domain not in {"godigit.com", *config.BROKER_EMAIL_DOMAINS}:
            email = candidate

    # ── Vehicle Number ─────────────────────────────────────────────────────
    veh_m = re.search(r"Vehicle\s+Registration\s+No\.?\s*\n\s*(NEW|[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4}|[A-Z0-9-]{4,15})", raw, re.IGNORECASE)
    if not veh_m:
        veh_m = re.search(r"Registration\s+Number\s*:\s*\n\s*(NEW|[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4}|[A-Z0-9-]{4,15})", raw, re.IGNORECASE)
    if not veh_m:
        veh_m = re.search(r"^[A-Z][A-Z .'-]+\s+(NEW|[A-Z]{2}[0-9][A-Z0-9]{4,})\s+[A-Z]", raw, re.MULTILINE)
    raw_veh = veh_m.group(1).strip() if veh_m else ""
    vehicle_number = "NEW" if raw_veh.upper() == "NEW" else _normalize_vehicle_number(raw_veh)

    # ── Make, Model, Engine, Chassis, Fuel, GVW ────────────────────────────
    make = ""
    model_variant = ""
    engine_number = ""
    chassis_number = ""
    fuel_type = ""
    gvw = ""

    if is_commercial:
        # Commercial vehicle details
        if not make:
            mk_m = re.search(r"(?:^|\n)Make\s*\n\s*([^\n]+)", raw)
            if mk_m:
                make = mk_m.group(1).strip()
        if not engine_number:
            en_m = re.search(r"Engine\s+(?:No\.?|Number)\s*[:\n]+\s*([A-Za-z0-9]+)", raw, re.IGNORECASE)
            if en_m:
                engine_number = en_m.group(1).strip()
        if not chassis_number:
            ch_m = re.search(r"Chassis\s+(?:No\.?|Number)\s*[:\n]+\s*([A-Za-z0-9]+)", raw, re.IGNORECASE)
            if ch_m:
                chassis_number = ch_m.group(1).strip()
        if not fuel_type:
            fl_m = re.search(r"Fuel\s+Type\s*\n\s*([A-Za-z]+)", raw)
            if fl_m:
                fuel_type = fl_m.group(1).strip()
        if not gvw:
            gvw_m = re.search(r"\b(\d{3,5})\s*KG\b", raw, re.IGNORECASE)
            if gvw_m:
                gvw = gvw_m.group(1).strip()

        # Model / Variant extraction via candidate filtering in vehicle section
        sec_m = re.search(r"YOUR VEHICLE DETAILS(.*?)YOUR POLICY DETAILS|YOUR VEHICLE DETAILS(.*?)(?:Period of Policy|Gross Premium|Total IDV|\Z)", raw, re.DOTALL | re.IGNORECASE)
        sec = sec_m.group(0) if sec_m else raw
        v_lines = [l.strip() for l in sec.splitlines() if l.strip()]
        ignore_set = {
            "YOUR VEHICLE DETAILS", "MAKE", "TRAILER REG. NO.", "RTO LOCATION",
            "MODEL/VEHICLE VARIANT", "(SUB-TYPE)", "YEAR OF REGN. /", "YEAR OF REGN.", "MANUFACTURING",
            "LICENSED SEATING", "CAPACITY", "ENGINE NO.", "CHASSIS NO.", "POWER",
            "FUEL TYPE", "GROSS VEHICLE WEIGHT", "CUBIC CAPACITY", "VEHICLE BODY TYPE",
            "GOODS TYPE", "PERMIT TYPE", "WHEELS", "PUBLIC CARRIER", "PRIVATE CARRIER",
            "--", "ELECTRIC", "DIESEL", "PETROL", "CNG", make.upper(), engine_number.upper(), chassis_number.upper()
        }
        candidates = []
        for l in v_lines:
            u = l.upper()
            if u in ignore_set:
                continue
            if re.match(r"^[0-9]+$", l):
                continue
            if re.match(r"^/\s*\d{4}-$", l):
                continue
            if re.match(r"^[A-Z]\d{2}$", l):
                continue
            if re.search(r"\b(?:KW|CC|KG)\b", l, re.IGNORECASE):
                continue
            candidates.append(l)

        if candidates:
            model_variant = candidates[0]
            if (model_variant.endswith("/") or model_variant.endswith("&")) and len(candidates) > 1:
                model_variant = f"{model_variant} {candidates[1]}"
    else:
        # Private car layout
        make_m = re.search(r"(?:^|\n)Make\s*\n\s*([A-Za-z][A-Za-z /&-]+)", raw, re.MULTILINE)
        if make_m:
            make = make_m.group(1).strip()
            make = re.sub(r"\s+(?:Model|Fuel|Engine|Chassis|Seating|Year|Electrical|Non|Trailer).*", "", make, flags=re.IGNORECASE).strip()
            
        model_m = re.search(r"Model/Vehicle Variant\s*(?:\(Sub-\s*\n?\s*Type\))?\s*\n\s*([^\n]+)", raw)
        if model_m:
            model_variant = model_m.group(1).strip()
            if model_variant.startswith("(Sub-Type)") or not model_variant:
                m2 = re.search(r"Model/Vehicle Variant[^\n]*\n(?:\(Sub-Type\)\s*\n)?\s*([^\n]+)", raw)
                if m2:
                    model_variant = m2.group(1).strip()
            if model_variant.endswith("/") or model_variant.endswith("&"):
                m_next = re.search(re.escape(model_variant) + r"\s*\n\s*([^\n]+)", raw)
                if m_next:
                    model_variant = f"{model_variant} {m_next.group(1).strip()}"

        fuel_m = re.search(r"Fuel\s+Type\s*\n\s*([A-Za-z/]+)", raw)
        fuel_type = fuel_m.group(1).strip() if fuel_m else ""

        eng_m = re.search(r"Engine\s+(?:No\.?|Number)\s*[:\n]+\s*([A-Za-z0-9]+)", raw, re.IGNORECASE)
        engine_number = eng_m.group(1).strip() if eng_m else ""

        chassis_m = re.search(r"Chassis\s+(?:No\.?|Number)\s*[:\n]+\s*([A-Za-z0-9]+)", raw, re.IGNORECASE)
        chassis_number = chassis_m.group(1).strip() if chassis_m else ""

    # ── Year of Manufacture ────────────────────────────────────────────────
    yr_m = re.search(r"Year\s+of\s+Regn\.?/\s*\n\s*Manufacturing\s*\n\s*(\d{4})", raw, re.IGNORECASE)
    if not yr_m:
        yr_m = re.search(r"Year\s+of\s+Regn(?:/Year\s+of\s+Mfg\.?)?\s*\n\s*(\d{4})", raw, re.IGNORECASE)
    if not yr_m:
        yr_m = re.search(r"Year\s+of\s+Regn\.?\s*\n\s*(\d{4})", raw, re.IGNORECASE)
    if not yr_m:
        yr_m = re.search(r"Manufacturing\s+Year\s+(?:and\s+Month\s+)?(\d{4})", raw, re.IGNORECASE)
    if not yr_m:
        yr_m = re.search(r"Year\s+of\s+Regn\.?\s*/\s*\n\s*Manufacturing\s*\n\s*--\s*\n\s*/\s*(\d{4})-", raw, re.IGNORECASE)
    year_of_manufacture = ""
    if yr_m and yr_m.group(1) != "0001":
        year_of_manufacture = yr_m.group(1)

    # ── CC / Power ─────────────────────────────────────────────────────────
    cc_m = re.search(r"Cubic\s+Capacity\s*\n\s*([0-9]+)\s*CC", raw, re.IGNORECASE)
    if not cc_m:
        cc_m = re.search(r"([0-9]+)\s*CC\s*\n\s*Cubic\s+Capacity", raw, re.IGNORECASE)
    if not cc_m:
        cc_m = re.search(r"([0-9.]+)\s*KW\s*\n\s*Power", raw, re.IGNORECASE)
    cc_kwh = cc_m.group(1) if cc_m else ""

    # ── Seating Capacity ───────────────────────────────────────────────────
    seat_m = re.search(r"(?:Licensed\s+)?Seating\s+Capacity\s*\n\s*([0-9]+)", raw, re.IGNORECASE)
    passenger = seat_m.group(1).strip() if seat_m else ""

    # ── RTO ────────────────────────────────────────────────────────────────
    rto_m = re.search(r"RTO\s+Location\s*\n\s*([^\n]+)", raw)
    rto_name = rto_m.group(1).strip() if rto_m else ""
    if rto_name.startswith("Trailer") or rto_name.startswith("Model/Vehicle"):
        rto_m2 = re.search(r"([A-Za-z\s,]+)\s*\n\s*YOUR VEHICLE DETAILS", raw)
        if rto_m2:
            rto_name = rto_m2.group(1).strip().replace("\n", " ")

    # ── Policy Dates ───────────────────────────────────────────────────────
    def _gd_date(s: str) -> str:
        s = s.strip()
        m_short = re.match(r"(\d{2})-([A-Za-z]{3})-(\d{4})", s)
        if m_short:
            months = {"Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06",
                      "Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12"}
            mo = months.get(m_short.group(2).capitalize(), "00")
            return f"{m_short.group(1)}/{mo}/{m_short.group(3)}"
        m_iso = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
        if m_iso:
            return f"{m_iso.group(3)}/{m_iso.group(2)}/{m_iso.group(1)}"
        return s.replace("-", "/")

    policy_issue_date = ""
    policy_start = ""
    policy_end = ""

    iss_head = re.search(r"\b[A-Z][0-9]{7,10}\s*/\s*(\d{2})(\d{2})(\d{4})\b", raw)
    if iss_head:
        policy_issue_date = f"{iss_head.group(1)}/{iss_head.group(2)}/{iss_head.group(3)}"

    wm_dates = re.search(r"\b(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})\b", raw)
    if wm_dates:
        policy_start = _gd_date(wm_dates.group(1))
        policy_end = _gd_date(wm_dates.group(2))

    if not policy_start or not policy_end:
        m_details = re.search(r"YOUR POLICY DETAILS(.*?)YOUR VEHICLE DETAILS", raw, re.DOTALL | re.IGNORECASE)
        if m_details:
            block = m_details.group(1)
            dates_in_block = re.findall(r"\b(\d{2}-[A-Za-z]{3}-\d{4})\b", block)
            if len(dates_in_block) == 3:
                if not policy_issue_date:
                    policy_issue_date = _gd_date(dates_in_block[0])
                policy_start = _gd_date(dates_in_block[1])
                policy_end = _gd_date(dates_in_block[2])
            elif len(dates_in_block) == 4:
                if "From" in block and "To" in block:
                    policy_start = _gd_date(dates_in_block[0])
                    policy_end = _gd_date(dates_in_block[2])
                    if not policy_issue_date:
                        policy_issue_date = _gd_date(dates_in_block[1])
            elif len(dates_in_block) >= 6:
                policy_start = _gd_date(dates_in_block[2])
                policy_end = _gd_date(dates_in_block[3])

    if not policy_start or not policy_end:
        m_triple = re.search(r"(\d{2}-[A-Za-z]{3}-\d{4})\s*\n\s*(\d{2}-[A-Za-z]{3}-\d{4})\s*\n\s*(\d{2}-[A-Za-z]{3}-\d{4})", raw)
        if m_triple:
            if not policy_issue_date:
                policy_issue_date = _gd_date(m_triple.group(1))
            policy_start = _gd_date(m_triple.group(2))
            policy_end = _gd_date(m_triple.group(3))

    if not policy_issue_date:
        iss_m = re.search(r"Policy\s+Issue\s+Date[^\n]*\n(?:[^\n]*\n){0,3}\s*(\d{2}-[A-Za-z]{3}-\d{4}|\d{4}-\d{2}-\d{2})", raw, re.IGNORECASE)
        if not iss_m:
            iss_m = re.search(r"Invoice\s+Date[^\n]*\n(?:[^\n]*\n){0,3}\s*(\d{2}-[A-Za-z]{3}-\d{4}|\d{4}-\d{2}-\d{2})", raw, re.IGNORECASE)
        if iss_m:
            policy_issue_date = _gd_date(iss_m.group(1))
        else:
            policy_issue_date = policy_start

    txn_date = policy_issue_date

    # ── NCB ────────────────────────────────────────────────────────────────
    ncb_m = re.search(r"NCB\s*%\s*\(Current\s+Policy\)\s*\n\s*(\d+)\s*%", raw, re.IGNORECASE)
    if not ncb_m:
        ncb_label_m = re.search(r"NCB\s*%\s*\(Current\s+Policy\)", raw, re.IGNORECASE)
        if ncb_label_m:
            window = raw[ncb_label_m.end():ncb_label_m.end()+300]
            ncb_val_m = re.search(r"(\d+)\s*%", window)
            if ncb_val_m:
                ncb_m = ncb_val_m
    current_ncb = ncb_m.group(1) if ncb_m else "0"

    # ── IDV ────────────────────────────────────────────────────────────────
    idv = "0"
    if not is_tp_only:
        m_idv = re.search(r'Total\s+IDV.*?(\d{5,})', raw, re.DOTALL | re.I)
        if not m_idv:
            m_idv = re.search(r'Vehicle\s+IDV.*?(\d{5,})', raw, re.DOTALL | re.I)
        if not m_idv:
            m_idv = re.search(r'Year\s*1.*?(\d{5,})', raw, re.DOTALL | re.I)
        if m_idv:
            idv = m_idv.group(1).replace(",", "")

    # ── Premiums & Endorsement Table ──────────────────────────────────────
    od_net_premium = "0"
    tp_net_premium = "0"
    net_premium = ""
    gross_premium = ""
    gst = ""

    # 1. Parse GST components from all IGST/CGST/SGST lines
    gst_components = []
    for l in raw.splitlines():
        if any(w in l.upper() for w in ['IGST', 'CGST', 'SGST', 'UTGST']) and ('@' in l or '%' in l or '=' in l):
            comp_matches = re.findall(r'=\s*\(?`?([0-9,]+\.[0-9]{2})', l)
            for c in comp_matches:
                try:
                    gst_components.append(float(c.replace(',', '')))
                except ValueError:
                    pass
    if gst_components:
        gst = f"{sum(gst_components):.2f}"

    # 2. Endorsement table
    end_m = re.search(r'IA\d+\s*\n\s*\d{4}-\d{2}-\d{2}\s*\n\s*([0-9.]+)\s*\n\s*([0-9.]+)\s*\n\s*([0-9.]+)\s*\n\s*([0-9.]+)\s*\n\s*([0-9.]+)\s*\n\s*([0-9.]+)\s*\n\s*([0-9.]+)', raw)
    if end_m and float(end_m.group(7)) > 0:
        net_premium = end_m.group(1)
        gross_premium = end_m.group(7)

    # 3. Comprehensive Car schedule table
    od_tp_car = re.search(
        r"Total\s+OD\s*Premium\s*\n\s*Total\s+Act\s*Premium\s*\n(?:\s*\(`\)\s*\n)?\s*([0-9,]+\.[0-9]{2})\s*\n(?:\s*\(`\)\s*\n)?\s*([0-9,]+\.[0-9]{2})",
        raw, re.IGNORECASE
    )
    if od_tp_car:
        od_net_premium = od_tp_car.group(1).replace(",", "")
        tp_net_premium = od_tp_car.group(2).replace(",", "")

    # 4. Commercial layout
    if is_commercial:
        comm_od_tp = re.search(
            r"([0-9,]+\.[0-9]{2})\s*\n\s*([0-9,]+\.[0-9]{2})\s*\n\s*Total\s+OD\s*\n\s*Premium\s*\n\s*Total\s+Act\s*\n\s*Premium",
            raw, re.IGNORECASE
        )
        if comm_od_tp:
            tp_net_premium = comm_od_tp.group(1).replace(",", "")
            od_net_premium = comm_od_tp.group(2).replace(",", "")
        else:
            od_comm = re.search(r"Own\s+Damage\s+Premium\s*\(`\)\s*\n\s*([0-9,]+\.[0-9]{2})", raw, re.IGNORECASE)
            if od_comm:
                od_net_premium = od_comm.group(1).replace(",", "")
            tp_comm = re.search(r"Basic\s+Third-Party\s+Liability\s*\(`\)\s*\n\s*([0-9,]+\.[0-9]{2})", raw, re.IGNORECASE)
            if tp_comm:
                tp_val = float(tp_comm.group(1).replace(",", ""))
                pa_d = re.search(r"PA\s+cover\s+for\s+Owner-Driver\s*\(`\)\s*\n\s*([0-9,]+\.[0-9]{2})", raw)
                if pa_d:
                    tp_val += float(pa_d.group(1).replace(",", ""))
                dr_d = re.search(r"Legal\s+Liability\s+to\s+Paid\s*\(`\)\s*Driver[^\n]*\n\s*([0-9,]+\.[0-9]{2})", raw)
                if dr_d:
                    tp_val += float(dr_d.group(1).replace(",", ""))
                tp_net_premium = f"{tp_val:.2f}"

        if not net_premium or not gross_premium:
            c_net = re.search(r'([0-9,]+\.[0-9]{2})\s*\n\s*([0-9,]+\.[0-9]{2})\s*\n\s*Net\s+Premium\s*\[A\+B[\]\}]', raw, re.I)
            if c_net:
                gross_premium = c_net.group(1).replace(",", "")
                net_premium = c_net.group(2).replace(",", "")

    # 5. SAOD
    if is_saod:
        tp_net_premium = "0"
        if not net_premium:
            saod_od = re.search(r"([0-9,]+\.[0-9]{2})\s*\n\s*([0-9,]+\.[0-9]{2})\s*\n(?:\s*\(`\)\s*\n)?\s*Net\s+Premium\s*\n\s*Final\s+Premium", raw, re.I)
            if saod_od:
                od_net_premium = saod_od.group(2).replace(",", "")
                net_premium = od_net_premium
            else:
                saod_od2 = re.search(r"Total\s+OD\s*Premium\s*\n(?:\s*\(`\)\s*\n)?\s*([0-9,]+\.[0-9]{2})", raw, re.IGNORECASE)
                if saod_od2:
                    od_net_premium = saod_od2.group(1).replace(",", "")
                    net_premium = od_net_premium
        else:
            od_net_premium = net_premium

        if not gross_premium:
            saod_gross = re.search(r"([0-9,]+\.[0-9]{2})\s*\n\s*([0-9,]+\.[0-9]{2})\s*\n\s*Net\s+Premium\s*\n\s*Final\s+Premium", raw, re.I)
            if saod_gross:
                gross_premium = saod_gross.group(2).replace(",", "")

    # 6. TP-Only
    if is_tp_only:
        od_net_premium = "0"
        idv = "0"
        if not net_premium:
            tp_m = re.search(r"([0-9,]+\.[0-9]{2})\s*\n\s*([0-9,]+\.[0-9]{2})\s*\n(?:\s*\(`\)\s*\n)?\s*Net\s+Premium\s*\n\s*Final\s+Premium", raw, re.I)
            if tp_m:
                tp_net_premium = tp_m.group(1).replace(",", "")
                net_premium = tp_net_premium
            else:
                tp_only_m = re.search(r"Basic\s+Third-Party\s+Liability\s*(?:\n\s*\(`\))?\s*\n\s*([0-9,]+\.[0-9]{2})", raw, re.IGNORECASE)
                if tp_only_m:
                    tp_net_premium = tp_only_m.group(1).replace(",", "")
                    net_premium = tp_net_premium
        else:
            tp_net_premium = net_premium

        if not gross_premium:
            tp_gross = re.search(r"([0-9,]+\.[0-9]{2})\s*\n\s*([0-9,]+\.[0-9]{2})\s*\n\s*Net\s+Premium\s*\n\s*Final\s+Premium", raw, re.I)
            if tp_gross:
                gross_premium = tp_gross.group(2).replace(",", "")

    # 7. Fallback calculations between Net, Gross, GST
    if not net_premium and od_net_premium != "0" and tp_net_premium != "0":
        try:
            net_premium = f"{float(od_net_premium) + float(tp_net_premium):.2f}"
        except ValueError:
            pass
    elif not net_premium and od_net_premium != "0":
        net_premium = od_net_premium
    elif not net_premium and tp_net_premium != "0":
        net_premium = tp_net_premium

    if not gross_premium and net_premium and gst:
        try:
            gross_premium = f"{float(net_premium) + float(gst):.2f}"
        except ValueError:
            pass

    if not gst and net_premium and gross_premium:
        try:
            diff = float(gross_premium) - float(net_premium)
            if diff > 0:
                gst = f"{diff:.2f}"
        except ValueError:
            pass

    payment_amount = gross_premium

    # ── CPA ───────────────────────────────────────────────────────────────
    cpa = "No"
    pa_from_m = re.search(r"Period of Policy for\s*\nPA Owner-Driver\s*\nTo\s*\n(.{1,30})\n", raw, re.IGNORECASE)
    if pa_from_m:
        pa_val = pa_from_m.group(1).strip()
        if pa_val not in ("--", "-", "", "NA") and re.search(r"\d", pa_val):
            cpa = "Yes"
    if cpa == "No":
        pa_m2 = re.search(r"PA\s+cover\s+for\s+Owner-Driver[^\n]*\n(?:\s*\(`\)\s*\n)?\s*([0-9,]+(?:\.[0-9]{1,2})?)", raw, re.IGNORECASE)
        if pa_m2:
            try:
                if float(pa_m2.group(1).replace(",", "")) > 0:
                    cpa = "Yes"
            except ValueError:
                pass

    # ── Payment Mode ──────────────────────────────────────────────────────
    pm_m = re.search(r"Payment\s+Mode\s*\n\s*(Online|Cheque|NEFT|UPI|Cash)", raw, re.IGNORECASE)
    payment_mode = pm_m.group(1).strip().title() if pm_m else "Online"

    cheque_number = ""
    if payment_mode.lower() == "cheque":
        ch_m = re.search(r"Cheque/Trans(?:action|ation)\s+No\s*\n\s*([A-Za-z0-9]+)", raw, re.IGNORECASE)
        cheque_number = ch_m.group(1).strip() if ch_m else ""

    # ── Business Type ─────────────────────────────────────────────────────
    business_type = _determine_go_digit_business_type(raw)

    # ── Normalise numeric fields ──────────────────────────────────────────
    def _norm_num(v: str) -> str:
        if v and re.match(r"^\d+\.00$", v):
            return v[:-3]
        return v

    od_net_premium  = _norm_num(od_net_premium)
    tp_net_premium  = _norm_num(tp_net_premium)
    idv             = _norm_num(idv)
    gst             = _norm_num(gst)
    payment_amount  = _norm_num(payment_amount)
    net_premium     = _norm_num(net_premium)
    gross_premium   = _norm_num(gross_premium)

    return {
        "policy_number":       policy_number,
        "customer_name":       customer_name,
        "address":             address,
        "city":                city,
        "state":               state,
        "pin":                 pin,
        "phone_number":        phone_number,
        "mobile_number":       mobile_number,
        "dob":                 "",
        "email":               email,
        "insurer":             insurer,
        "business_type":       business_type,
        "policy_type":         policy_type,
        "vehicle_number":      vehicle_number,
        "make":                make,
        "model_variant":       model_variant,
        "year_of_manufacture": year_of_manufacture,
        "chassis_number":      chassis_number,
        "engine_number":       engine_number,
        "cc_kwh":              cc_kwh,
        "fuel_type":           fuel_type,
        "rto_name":            rto_name,
        "current_ncb":         current_ncb,
        "previous_ncb":        "0",
        "passenger":           passenger,
        "gvw":                 gvw,
        "policy_issue_date":   policy_issue_date,
        "policy_start":        policy_start,
        "policy_end":          policy_end,
        "txn_date":            txn_date,
        "idv":                 idv,
        "od_net_premium":      od_net_premium,
        "tp_net_premium":      tp_net_premium,
        "gst":                 gst,
        "gross_premium":       gross_premium,
        "net_premium":         net_premium,
        "cpa":                 cpa,
        "payment_mode":        payment_mode,
        "cheque_number":       cheque_number,
        "bank_name":           "",
        "payment_amount":      payment_amount,
        "needs_review":        False,
        "review_reason":       None,
    }


def _extract_marked_page_text(text: str, page_number: int) -> str:
    raw_text = str(text or "")
    if page_number < 1:
        return ""

    page_pattern = re.compile(
        rf"\[\[PAGE\s+{page_number}\s+START\]\](.*?)\[\[PAGE\s+{page_number}\s+END\]\]",
        flags=re.IGNORECASE | re.DOTALL,
    )
    match = page_pattern.search(raw_text)
    if not match:
        return ""
    return match.group(1).strip()


def _extract_zuno_proposer_details_section(text: str) -> str:
    page_6_text = _extract_marked_page_text(text, 6)
    if not page_6_text:
        return ""

    match = re.search(
        r"Proposal\s+Details(.*?)(?:Policy\s+Period\s*\(OD\)|Vehicles\s+Details|Nominee\s+Details)",
        page_6_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return match.group(1).strip()

    return page_6_text


def _extract_zuno_policy_type_from_page_2(text: str) -> str:
    page_2_text = _extract_marked_page_text(text, 2)
    if not page_2_text:
        return ""

    header_match = re.search(
        r"Zuno\s+SmartDrive\s+Motor\s+Policy\s*[–—-]\s*([^\n\r]+)",
        page_2_text,
        flags=re.IGNORECASE,
    )
    if not header_match:
        return ""

    full_header = f"Zuno SmartDrive Motor Policy - {header_match.group(1).strip()}"
    return _normalize_policy_type(full_header, is_zuno=True)


def _go_digit_page_priority_rule(field_keys: list[str]) -> str:
    selected = set(field_keys)
    rules: list[str] = []
    if selected & _GO_DIGIT_PAGE_1_FIELDS:
        rules.append(
            "- Go Digit page priority: for insured/contact and vehicle fields, use the explicitly labelled Page 1 value first. "
            "Use a later-page copy only when Page 1 has no labelled value."
        )
    if "policy_number" in selected:
        rules.append(
            "- Go Digit page priority: for policy_number, prefer the first explicitly labelled policy schedule/certificate value. "
            "Do not let a later proposal, receipt, payment, or transaction number override it."
        )
    if selected & _GO_DIGIT_MIDDLE_PAGE_FIELDS:
        rules.append(
            "- Go Digit page priority: for policy period, cover, NCB, premium, tax, payment, and previous-policy fields, "
            "prefer the explicitly labelled detail-table values on Pages 2-3. Use Page 1 only if that field is absent there."
        )
    if selected & _GO_DIGIT_FINAL_PAGE_FIELDS:
        rules.append(
            "- Go Digit page priority: use a final-page receipt/transaction date only when that exact date label is present. "
            "Never substitute a policy start/end date."
        )
    return "\n".join(rules)


def _go_digit_field_rules(field_keys: list[str]) -> list[str]:
    selected = set(field_keys)
    rules: list[str] = []
    if selected & {"customer_name", "address", "city", "state", "pin"}:
        rules.append(
            "- Go Digit-specific: customer_name, address, city, state, and pin must come from the labelled insured/proposer address block. "
            "Keep address lines together; split city/state/pin only when each is explicitly shown."
        )
    if selected & {"phone_number", "mobile_number", "email", "dob"}:
        rules.append(
            "- Go Digit-specific: phone_number, mobile_number, email, and dob must come from the insured/proposer block only. "
            "Preserve masking characters in contact details, and return null when only insurer/support contacts are present."
        )
    if "policy_type" in selected:
        rules.append(
            "- Go Digit-specific: policy_type is the complete motor product/cover name in the schedule or certificate heading. "
            "Do not shorten it to vehicle class or infer a cover type."
        )
    if "year_of_manufacture" in selected:
        rules.append(
            "- Go Digit-specific: year_of_manufacture must be the labelled manufacturing year, not the policy, registration, or issue year."
        )
    if "cc_kwh" in selected:
        rules.append(
            "- Go Digit-specific: cc_kwh is the labelled engine capacity or electric KWH value only; return the numeric value without units."
        )
    if "rto_name" in selected:
        rules.append(
            "- Go Digit-specific: rto_name is the labelled registration place/RTO value, not a city copied from the insured address."
        )
    if "passenger" in selected:
        rules.append(
            "- Go Digit-specific: passenger is the labelled seating/passenger capacity. Return null when it is not printed."
        )
    if "gvw" in selected:
        rules.append(
            "- Go Digit-specific: gvw is the labelled gross vehicle weight and is normally inapplicable to private cars. Return null unless explicitly printed."
        )
    if selected & {"current_ncb", "previous_ncb"}:
        rules.append(
            "- Go Digit-specific: current_ncb is the discount applied in this policy's premium table; previous_ncb belongs only to previous-policy details. "
            "Return the percentage number without % and do not use a rupee discount amount."
        )
    if "cpa" in selected:
        rules.append(
            "- Go Digit-specific: cpa is 'Yes' only when a labelled compulsory/personal accident owner-driver cover has a non-zero premium; otherwise 'No'."
        )
    if "idv" in selected:
        rules.append(
            "- Go Digit-specific: idv is the labelled insured declared value, not the premium, liability limit, or vehicle price. Return null for liability-only cover when no IDV exists."
        )
    if "net_premium" in selected:
        rules.append(
            "- Go Digit-specific: net_premium is the labelled premium before GST, not a component premium and not the final inclusive amount."
        )
    if "gross_premium" in selected:
        rules.append(
            "- Go Digit-specific: gross_premium is the final total premium inclusive of GST only when explicitly labelled; otherwise return null."
        )
    if selected & {"payment_mode", "cheque_number", "bank_name", "payment_amount"}:
        rules.append(
            "- Go Digit-specific: payment_mode, cheque_number, bank_name, and payment_amount come only from labelled payment/receipt details. "
            "Populate cheque_number and bank_name only for Cheque mode; otherwise return null."
        )
    if selected & {"policy_issue_date", "txn_date"}:
        rules.append(
            "- Go Digit-specific: policy_issue_date and txn_date require their own explicit issue/receipt/transaction labels. "
            "Do not derive either from the policy period."
        )
    if selected & {"tp_start", "tp_end"}:
        rules.append(
            "- Go Digit-specific: tp_start and tp_end require an explicit Third Party/Liability period row. "
            "Return null when only the overall or Own Damage policy period is printed."
        )
    return rules


def _company_specific_prompt_rules(
    policy_text: str, field_keys: list[str], company_mode: str | None = None
) -> str:
    # Determine effective company: explicit selection overrides auto-detect
    if company_mode == "ZUNO" or (
        company_mode is None and _is_zuno_policy_document(policy_text)
    ):
        rules: list[str] = []
        if any(key in _ZUNO_PROPOSER_FIELD_KEYS for key in field_keys):
            rules.append(
                "- Zuno-specific robustness: for proposer fields "
                "(customer_name, address, city, state, pin, phone_number, mobile_number, dob, email), "
                "read ONLY from Page 6 under 'TRANSCRIPT OF THE PROPOSAL' -> 'Proposal Details'. "
                "Do not use proposer values from any other page."
            )
        if "business_type" in field_keys:
            rules.append(
                "- Zuno-specific robustness: for business_type, read from Page 6 under "
                "'TRANSCRIPT OF THE PROPOSAL' -> 'Product Details' -> 'Business Type' "
                "(e.g. Rollover, New Business, Renewal)."
            )
        if "policy_type" in field_keys:
            rules.append(
                "- Zuno-specific robustness: for policy_type, read ONLY from the large yellow "
                "header on Page 2 ('Zuno SmartDrive Motor Policy - ...'). "
                "Return only the portion after the last dash."
            )
        if "chassis_number" in field_keys:
            rules.append(
                "- Zuno-specific robustness: for chassis_number, extract the complete 17-character "
                "full alphanumeric code from Page 6 under Proposal Details -> Vehicles Details -> Chassis Number. "
                "Never truncate it or omit prefix characters."
            )
        if "payment_mode" in field_keys:
            rules.append(
                "- Zuno-specific: payment_mode is found in Page 6 under 'TRANSCRIPT OF THE PROPOSAL' -> "
                "'Payment Details' -> 'Payment Mode' (e.g. 'Online', 'Cheque', 'NEFT')."
            )
        if "cpa" in field_keys:
            rules.append(
                "- Zuno-specific: for cpa, look in the premium table on page 3 under 'B. Liability' -> 'PA' "
                "for a row labeled 'PA Owner Driver' or 'PA Cover for Owner Driver'. "
                "Return the numeric premium value for that specific row only. "
                "Do NOT return the unnamed passenger PA or paid driver values. "
                "If this row is absent, return '0'."
            )
        if "cheque_number" in field_keys:
            rules.append(
                "- Zuno-specific: cheque_number is found in Page 6 under 'TRANSCRIPT OF THE PROPOSAL' ->"
                " 'Payment Details' -> 'Instrument / Reference Number'. "
                "Return the alphanumeric value ONLY if Payment Mode is 'Cheque'; otherwise return empty string."
            )
        if "bank_name" in field_keys:
            rules.append(
                "- Zuno-specific: bank_name is found in Page 6 under 'TRANSCRIPT OF THE PROPOSAL' ->"
                " 'Payment Details' -> 'Cheque drawn on'. "
                "Return the value ONLY if Payment Mode is 'Cheque'; otherwise return empty string."
            )
        if "payment_amount" in field_keys:
            rules.append(
                "- Zuno-specific: payment_amount is found in Page 6 under 'TRANSCRIPT OF THE PROPOSAL' -> "
                "'Payment Details' -> 'Premium Amount'. Return just the numeric value."
            )
        return "\n".join(rules)

    elif company_mode == "HDFC ERGO" or (
        company_mode is None and _is_hdfc_policy_document(policy_text)
    ):
        rules = []
        if any(key in _ZUNO_PROPOSER_FIELD_KEYS for key in field_keys):
            rules.append(
                "- HDFC ERGO-specific: Customer name is labeled 'Insured Name', 'Name of Insured', "
                "or 'Name' in the insured/proposer details section (usually page 1 or 2 of the certificate). "
                "Do NOT return nominee names or agent names."
            )
            rules.append(
                "- HDFC ERGO-specific: Address is under 'Communication Address', 'Address of Insured', "
                "or 'Insured Address' in the certificate - NOT the insurer's registered office address."
            )
        if "policy_number" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: Policy number is labeled 'Policy No.' or 'Policy Number' "
                "in the top header of the policy schedule - it is a long alphanumeric string "
                "(e.g. '1111222233334444000'). Do not confuse it with the proposal number. "
                "Remove any spaces from the policy number. It must be returned as a single unbroken string."
            )
        if "phone_number" in field_keys or "mobile_number" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: Phone/mobile numbers are printed next to 'Tel.' in the "
                "communication address block and may contain masking characters such as 'X' "
                "(e.g. '98XXXXXXX0'). Copy the number EXACTLY as it appears, preserving all "
                "X characters; do NOT replace them with digits or omit them. "
                "Use this same Tel. number for both phone_number and mobile_number fields "
                "since HDFC does not print a separate mobile field."
            )
        if "business_type" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: for business_type, look for 'Business Type' or 'Policy Type' "
                "field in the policy schedule header (e.g. 'New', 'Renewal', 'Rollover')."
            )
        if "policy_type" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: for policy_type, read the FULL plan name from the bold "
                "subtitle/header line that appears immediately below 'Certificate of Insurance "
                "cum Policy Schedule' or 'Motor Insurance' at the very top of the document. "
                "Examples: 'Standalone Motor Own Damage Cover - Private Car', "
                "'Private Car Comprehensive Policy', 'Motor Insurance - Passenger Carrying "
                "Comprehensive Policy', 'Motor Private Car - Long Term Policy'. "
                "Return the COMPLETE subtitle exactly as printed; do NOT shorten it to just "
                "'Private Car' or any other abbreviated form."
            )
        if "current_ncb" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: for current_ncb, extract the NCB percentage from the "
                "PREMIUM DETAILS TABLE only: look for the row labeled "
                "'Less: No Claim Bonus (X%)' or 'Less: No Claim Bonus X%' and return X as a plain number "
                "(e.g. '20' for 20%). "
                "Do NOT use the 'NCB' value that appears in the Previous Policy row at the bottom of the "
                "schedule; that is the previous year's NCB, not the current applied discount."
            )
        if "od_net_premium" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: for od_net_premium, the exact label in the premium table is "
                "'Net Own Damage Premium (a)': the number on the NEXT line is the value. "
                "Do NOT use 'Basic Own Damage' or 'Total Package Premium'."
            )
        if "tp_net_premium" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: for tp_net_premium, the exact label is "
                "'Net Liability Premium (b)' or 'Net Liability Premium': the number on the NEXT line is the value. "
                "Do NOT use 'Basic Third Party Liability'."
            )
        if "cpa" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: for cpa, look in the Liability Premium(b) section of the premium table "
                "on page 1 for ANY rows that start with 'PA' or 'PA Cover'. This includes rows like "
                "'PA Owner Driver', 'PA Cover for Owner Driver (CSI)', 'PA Cover for Un-Named Persons', "
                "'PA Unnamed Passenger', 'PA to Paid Driver', etc. "
                "If ANY PA row exists with a non-zero amount, return 'Yes'. "
                "If no PA rows are present or all amounts are zero, return 'No'. "
                "Return ONLY 'Yes' or 'No', never a numeric amount."
            )
        if "fuel_type" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: Fuel type appears as a standalone label 'Fuel' followed "
                "by its value on the next line in the vehicle details table "
                "(e.g. 'Petrol', 'Diesel', 'CNG/Petrol', 'Electric', 'LPG'). "
                "If not explicitly labeled, look for 'CNG/LPG Kit' in the IDV column headers "
                "or the word Electric/CNG/Petrol/Diesel near the vehicle spec section."
            )
        if "business_type" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: 'Business Type' is NOT printed explicitly. "
                "Determine it using the following three-way logic:\n"
                "  1. New Business: No previous policy details (no 'Previous Policy No.' or "
                "     'Previous Insurer' fields) are present in the document.\n"
                "  2. Renewal: A 'Previous Policy No.' IS present AND the previous insurer "
                "     (from 'Previous Insurer', 'Previous Policy Insurer', or similar label) "
                "     is HDFC ERGO or HDFC General Insurance (i.e. the same company issued "
                "     the prior policy.\n"
                "  3. Rollover: A 'Previous Policy No.' IS present AND the previous insurer "
                "     is a DIFFERENT company (not HDFC ERGO), e.g. Bajaj Allianz, ICICI "
                "     Lombard, Tata AIG, New India, National Insurance, etc.\n"
                "Return exactly one of: 'New Business', 'Renewal', or 'Rollover'."
            )
        if "net_premium" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: net_premium is the 'Total Premium' value at the bottom of "
                "the premium table (after GST). Look for 'Total Premium\n<number>'."
            )
        if "chassis_number" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: for chassis_number, extract the complete alphanumeric VIN "
                "from 'Chassis No.' or 'Chassis Number' in the vehicle details section. "
                "It is typically 17 characters. Never truncate."
            )
        if "insurer" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: insurer is always 'HDFC ERGO General Insurance Company Limited'."
            )
        if "idv" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: if the policy is a Third Party / Liability Only policy "
                "(policy name contains 'Liability Only', 'Liability Policy', 'TP Only', or similar), "
                "IDV does not exist: return '0' for idv. Do NOT return 'illustration' or any text value."
            )
        if "payment_mode" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: payment_mode is found in the 'Payment Details' row near the top "
                "of the schedule, labeled 'Payment Details'. Common values: 'Online', 'Cheque', 'NEFT', 'UPI'. "
                "Extract just the mode keyword."
            )
        if "cheque_number" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: cheque_number / instrument number appears in the 'Payment Details' "
                "line, e.g. 'Payment Details : YESIG12345678901'. Extract only the alphanumeric reference "
                "after the colon. If absent or not applicable, return empty string."
            )
        if "bank_name" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: bank_name appears in the 'Payment Details' line after 'Bank Name:' "
                "(e.g. 'HDFC BANK LTD'). Return the bank name exactly as printed. If absent, return empty string."
            )
        if "payment_amount" in field_keys:
            rules.append(
                "- HDFC ERGO-specific: payment_amount is the 'Total Premium' value from the bottom of the "
                "premium table (the final amount inclusive of GST). Return just the numeric value."
            )
        return "\n".join(rules)

    elif company_mode == "TATA AIG" or (
        company_mode is None and _is_tata_policy_document(policy_text)
    ):
        rules: list[str] = []
        if any(key in _ZUNO_PROPOSER_FIELD_KEYS for key in field_keys):
            rules.append(
                "- TATA AIG-specific: customer_name is labeled 'Name' in the 'Insured Details' block on Page 1. "
                "Do NOT return nominee names or agent names."
            )
            rules.append(
                "- TATA AIG-specific: Address is labeled 'Address' under 'Insured Details' on Page 1. "
                "Include the full multi-line address text until 'Contact No.' (e.g. including locality, city, state, pincode). "
                "Do NOT cut off after the first line. Do NOT return the insurer's registered office address in the header."
            )
            rules.append(
                "- TATA AIG-specific: city and state should be extracted from the Insured Details address block. "
                "Never extract promotional words like 'Download now', 'TATA AIG App', or 'Scan QR Code' as city."
            )
            rules.append(
                "- TATA AIG-specific: Contact No. is labeled 'Contact No.' in the 'Insured Details' block on Page 1. "
                "It may be masked (e.g. '+91 96**10**56'). Copy EXACTLY as it appears. "
                "Use this for both phone_number and mobile_number since TATA AIG prints only one contact field."
            )
            rules.append(
                "- TATA AIG-specific: Email ID is labeled 'Email ID' in the 'Insured Details' block on Page 1. "
                "It may be masked (e.g. 'Anu***@gmail.com'). Copy EXACTLY as it appears."
            )
        if "policy_number" in field_keys:
            rules.append(
                "- TATA AIG-specific: Policy number is labeled 'Policy No.' in the 'Insured Details' block on Page 1. "
                "It is printed with spaces (e.g. '9876543210 01 00'). Remove ALL spaces and return as a "
                "single unbroken string (e.g. '9876543210 01 00' → '987654321001 00' → '98765432100100')."
            )
        if "policy_type" in field_keys:
            rules.append(
                "- TATA AIG-specific: policy_type is the product name from the bold header. "
                "Return only the text AFTER the hyphen, e.g. for 'Auto Secure - Private Car Package Policy' "
                "return 'Private Car Package Policy'. Do not include 'Auto Secure' or the hyphen."
            )
        if "policy_start" in field_keys or "policy_end" in field_keys:
            rules.append(
                "- TATA AIG-specific: policy_start and policy_end are in the 'Period of Insurance & Premium' "
                "table on Page 1, in the 'Own Damage Cover' row (or 'Third-Party Cover' row if OD is absent). "
                "Use 'Valid From' as policy_start and 'Valid Till' as policy_end. "
                "Strip any time component like '(00:00 Hrs)' or '(Midnight)'; return date only in dd/mm/yyyy."
            )
        if "policy_issue_date" in field_keys:
            rules.append(
                "- TATA AIG-specific: policy_issue_date is the 'Receipt Date' field on the Receipt page "
                "(near the end of the document), labeled 'Receipt Date'. Format: dd/mm/yyyy."
            )
        if "vehicle_number" in field_keys:
            rules.append(
                "- TATA AIG-specific: vehicle registration number is labeled 'Registration No.' in the "
                "'Certificate of Insurance Cum Policy Schedule' section (Page 4). "
                "Return it without hyphens or spaces, e.g. 'HR19E1234' or 'DL07CQ1234'."
            )
        if "make" in field_keys or "model_variant" in field_keys:
            rules.append(
                "- TATA AIG-specific: Make, Model, and Variant appear under a single label 'Make / Model / Variant' "
                "in the vehicle details table (Page 4). The value may be wrapped across lines "
                "(e.g. 'MAHINDRA & MAHIND\\nRA / SCORPIO N / Z6 DI\\nESEL MT 2WD 7 STR'). "
                "Reconstruct the full string by joining lines. Then split by '/' to get: "
                "make (first part), model (second part), variant (third part). "
                "Return make as the manufacturer name and model_variant as 'Model / Variant' combined."
            )
        if "chassis_number" in field_keys:
            rules.append(
                "- TATA AIG-specific: chassis_number is labeled 'Chassis No.' in the vehicle details table (Page 4). "
                "It is a 17-character alphanumeric VIN. Copy it exactly, never truncate."
            )
        if "engine_number" in field_keys:
            rules.append(
                "- TATA AIG-specific: engine_number is labeled 'Engine Number / Motor No. (for EV)' in the "
                "vehicle details table (Page 4)."
            )
        if "cc_kwh" in field_keys:
            rules.append(
                "- TATA AIG-specific: CC/KW is labeled 'CC/KW' in the vehicle details table (Page 4). "
                "Return the numeric value only (e.g. '2184')."
            )
        if "passenger" in field_keys:
            rules.append(
                "- TATA AIG-specific: seating capacity is labeled 'Seating Capacity (Including Driver)' "
                "in the vehicle details table (Page 4). Return the numeric value."
            )
        if "rto_name" in field_keys:
            rules.append(
                "- TATA AIG-specific: RTO location is labeled 'RTO Location' in the 'Zone Details' sub-table "
                "within the vehicle details section (Page 4)."
            )
        if "idv" in field_keys:
            rules.append(
                "- TATA AIG-specific: IDV is the 'Total IDV (₹)' value in the 'Insured Declared Value (IDV) ₹ Details' "
                "table (Page 4). Return the numeric value for Policy Year 1. "
                "If the policy is a Third Party / Liability Only policy, return '0'."
            )
        if "od_net_premium" in field_keys:
            rules.append(
                "- TATA AIG-specific: od_net_premium is the sum of Own Damage (Section A) + Add-ons (Section B or C) + Others (Section C or D). "
                "For Package policies: A + C + D ('Total Own Damage Premium (A)' + 'Total Add on Premium (C)' + 'Total Others (D)'). "
                "For Standalone Own Damage (SAOD) policies: A + B + C ('Total Own Damage Premium (A)' + 'Total Add on Premium (B)' + 'Total Others (C)'), "
                "which equals Net Premium since TP is 0. Do NOT extract only Section A; you MUST include all Add-on covers and Others."
            )
        if "tp_net_premium" in field_keys:
            rules.append(
                "- TATA AIG-specific: tp_net_premium = Total Liability Premium (B) (+ Section E if present) from the 'Schedule of Premium' section. "
                "For Standalone Own Damage (SAOD) policies, tp_net_premium is 0. "
                "Do NOT include add-on amounts or Section A/C/D values in this figure."
            )
        if "current_ncb" in field_keys:
            rules.append(
                "- TATA AIG-specific: current_ncb is the percentage from the 'Less: No claim bonus (X%)' row "
                "in the 'Schedule of Premium' section (Page 5). Return X as a plain number (e.g. '20' for 20%). "
                "If no NCB row exists, return '0'."
            )
        if "previous_ncb" in field_keys:
            rules.append(
                "- TATA AIG-specific: previous_ncb is the 'NCB in Previous Policy: X%' value from the "
                "'Transcript Of Proposal' → 'Previous Insurance Details' section. Return X as a plain number."
            )
        if "cpa" in field_keys:
            rules.append(
                "- TATA AIG-specific: for cpa, check 'Compulsory PA Cover for Owner-Driver' in 'Period of Insurance & Premium' (Page 1) "
                "and 'Schedule of Premium' (Page 5). Return 'Yes' ONLY if an active date period or positive premium is stated. "
                "If it indicates 'NA', 'NA to NA', 'None', or is a company/corporate insured without CPA, return 'No'."
            )
        if "gst" in field_keys:
            rules.append(
                "- TATA AIG-specific: GST may appear as separate CGST@9% and SGST/UGST@9% rows "
                "(sum them together), or as a single IGST@18% row. Return the TOTAL GST as a plain number."
            )
        if "payment_mode" in field_keys:
            rules.append(
                "- TATA AIG-specific: payment_mode is in the Receipt table (around pages 10–11). "
                "Look for the column labelled 'Mode of Payment' and read the value in the data row beneath it "
                "(e.g. 'paymentLinkCustomer', 'onlinePayment', 'Cheque'). Normalise: "
                "any online/UPI/NEFT/IMPS variant → 'Online'; 'Cheque' → 'Cheque'; anything else → title-case."
            )
        if "payment_amount" in field_keys:
            rules.append(
                "- TATA AIG-specific: payment_amount is the 'Total Premium (₹) (Including GST)' in the Receipt table, "
                "or the 'Premium Amount (Including GST)' from Page 1. Return just the numeric value."
            )
        if "cheque_number" in field_keys:
            rules.append(
                "- TATA AIG-specific: cheque_number is not explicitly printed for online payments. "
                "Return empty string unless Mode of Payment is 'Cheque'."
            )
        if "business_type" in field_keys:
            rules.append(
                "- TATA AIG-specific: business_type is NOT printed explicitly. "
                "Determine it from the 'Transcript Of Proposal' → 'Previous Insurance Details' section:\n"
                "  1. New Business: No 'Previous Insurance Details' section or no previous insurer/policy number.\n"
                "  2. Renewal: A previous policy number exists AND 'Name of the Insurer' is 'TATA AIG General Insurance' or similar TATA AIG name.\n"
                "  3. Rollover: A previous policy number exists AND 'Name of the Insurer' is a DIFFERENT company (e.g. Go Digit, Bajaj Allianz, ICICI Lombard, etc.).\n"
                "Return exactly one of: 'New Business', 'Renewal', or 'Rollover'."
            )
        if "insurer" in field_keys:
            rules.append(
                "- TATA AIG-specific: insurer is always 'TATA AIG General Insurance Company Limited'."
            )
        return "\n".join(rules)

    elif company_mode == "GO DIGIT" or (
        company_mode is None and _is_go_digit_policy_document(policy_text)
    ):
        rules = [
            "- Go Digit-specific: treat repeated headers, footers, and any watermark text as document noise. "
            "Extract values only when they are associated with an explicit field label or table row. "
            "Never copy a value merely because it appears near a repeated schedule heading."
        ]
        page_priority_rule = _go_digit_page_priority_rule(field_keys)
        if page_priority_rule:
            rules.append(page_priority_rule)
        rules.extend(_go_digit_field_rules(field_keys))
        if any(key in _ZUNO_PROPOSER_FIELD_KEYS for key in field_keys):
            rules.append(
                "- Go Digit-specific: prefer the insured/proposer details block on Page 1 for customer_name, address, "
                "city, state, pin, phone/mobile, dob, and email. If the same data is repeated later, use only the value "
                "attached to the insured/proposer label. Do not use nominee, intermediary, insurer, or support-contact details."
            )
        if "policy_number" in field_keys:
            rules.append(
                "- Go Digit-specific: policy_number is the policy schedule or certificate number. "
                "Accept labels such as 'Policy No.', 'Policy Number', or equivalent, and never return a proposal, receipt, payment, or transaction reference."
            )
        if "vehicle_number" in field_keys:
            rules.append(
                "- Go Digit-specific: vehicle_number is the registration number in the vehicle details, commonly shown under the standalone label 'Vehicle' on Page 1. "
                "Use that labelled value or an explicit Registration Number/Regn No. value, never a chassis or engine number. Return it without spaces or hyphens."
            )
        if any(key in {"make", "model_variant", "chassis_number", "engine_number", "fuel_type"} for key in field_keys):
            rules.append(
                "- Go Digit-specific: vehicle make/model, fuel type, chassis number, and engine number are in the Page 1 vehicle details. "
                "Extract a chassis_number only from the Chassis/VIN-labelled cell and engine_number only from the Engine-labelled cell; "
                "never swap them. Copy the full values exactly; a chassis/VIN is normally a 17-character alphanumeric value."
            )
        if "policy_start" in field_keys or "policy_end" in field_keys:
            rules.append(
                "- Go Digit-specific: policy_start and policy_end come from the Own Damage or overall policy-period row. "
                "Do not substitute third-party dates into these fields. Return dates only, without times."
            )
        if any(key in {"idv", "od_net_premium", "tp_net_premium", "gst", "payment_amount"} for key in field_keys):
            rules.append(
                "- Go Digit-specific: read IDV and premium values only from their labelled premium-table rows. "
                "Use the total Own Damage row for od_net_premium, the total Third Party/Liability row for tp_net_premium, "
                "and the final premium inclusive of GST for payment_amount. For GST, return the total tax amount, not a percentage or repeated header value."
            )
        if "business_type" in field_keys:
            rules.append(
                "- Go Digit-specific: determine business_type from previous-policy details: no previous policy means 'New Business'; "
                "a previous Go Digit policy means 'Renewal'; another insurer means 'Rollover'."
            )
        if "insurer" in field_keys:
            rules.append(
                "- Go Digit-specific: insurer is always 'Go Digit General Insurance Limited'."
            )
        return "\n".join(rules)

    elif company_mode == "ICICI Lombard" or (
        company_mode is None and _is_icici_policy_document(policy_text)
    ):
        rules = []
        if "policy_type" in field_keys:
            rules.append(
                "- ICICI Lombard-specific: policy_type comes from the banner line immediately after "
                "'CERTIFICATE OF INSURANCE CUM POLICY SCHEDULE' on Page 2 or 3. "
                "Confirmed exact banner texts: 'Private Car Package Policy', 'Stand-Alone Own Damage Private Car Insurance Policy', "
                "'Miscellaneous Vehicles Package Policy' (Commercial), 'Two Wheeler Vehicles Package Policy', or 'Stand-Alone Own Damage Two wheeler Insurance Policy'. "
                "Secondary signal from Product Code: 3001 = Private Car Package, 3001/O = Stand-Alone Own Damage Private Car, "
                "3005 = Two Wheeler Package, 3005/O = Stand-Alone Own Damage Two Wheeler, 3008 = Miscellaneous/Commercial. "
                "NEVER trust the PDF filename as SAOD documents are frequently misnamed."
            )
        if any(key in _ZUNO_PROPOSER_FIELD_KEYS for key in field_keys):
            rules.append(
                "- ICICI Lombard-specific: customer_name is labeled 'Name of the Insured' (Certificate page) or "
                "'Insured Name'/'Name of Insured' (Risk Assumption Letter, Page 1). Allow commercial entity names. "
                "Do NOT return nominee names."
            )
            rules.append(
                "- ICICI Lombard-specific: address is under 'Address' in the Insured Details block. "
                "city, state, and pin are derived from the last line of the address block (format: '<City>, <STATE> <PIN>') "
                "or from 'RTO Location' / 'RTO City' (format: '<STATE>-<CITY>'). pin is the trailing 6-digit number."
            )
            rules.append(
                "- ICICI Lombard-specific: mobile_number and email should be captured pre-masked exactly as printed "
                "(e.g. '99******01', 'PU***********@GMAIL.COM'). Do NOT un-mask or discard masked values. "
                "Exclude 'customersupport@icicilombard.com' which is ICICI's boilerplate support address."
            )
        if "business_type" in field_keys:
            rules.append(
                "- ICICI Lombard-specific: determine business_type from the 'Previous Policy Details' section on Page 1: "
                "1. 'New Business': No Previous Policy Details block present, or vehicle registration number is literally 'NEW'. "
                "2. 'Renewal': Previous Policy Details block present AND Previous Insurer Name is ICICI itself ('ICICI LOMBARD' or 'ICLB'). "
                "3. 'Rollover': Previous Policy Details block present AND Previous Insurer Name is a different company ('RSUND', 'BAJAJ ALIANZ', 'SBI', etc.). "
                "Return exactly one of: 'New Business', 'Renewal', or 'Rollover'."
            )
        if "model_variant" in field_keys or "make" in field_keys:
            rules.append(
                "- ICICI Lombard-specific: model_variant is taken directly from the 'Model' column in the vehicle table, "
                "which is already a combined make/model/variant string. make is from the 'Make' column."
            )
        if "fuel_type" in field_keys or "gvw" in field_keys:
            rules.append(
                "- ICICI Lombard-specific: fuel_type and gvw are NEVER printed by ICICI Lombard; leave them null/empty. "
                "In commercial vehicle tables, 'Carrying Capacity' is passenger/crew headcount, NOT vehicle weight; NEVER map it to gvw."
            )
        if "tp_start" in field_keys or "tp_end" in field_keys:
            rules.append(
                "- ICICI Lombard-specific: tp_start and tp_end are present ONLY on Stand-Alone Own Damage (SAOD) variants, "
                "found under 'Third Party Insurance Details' on Page 1 (under 'Third Party Insurance Period', e.g. 'Jun 20, 2024 to Jun 19, 2027'). "
                "Package and Commercial policies have no separate third party period; leave tp_start and tp_end blank."
            )
        if any(key in {"od_net_premium", "tp_net_premium", "gst", "net_premium", "gross_premium", "payment_amount"} for key in field_keys):
            rules.append(
                "- ICICI Lombard-specific premium fields (pre-tax net vs post-tax gross convention): "
                "od_net_premium is 'Total Own Damage Premium(A)'. "
                "tp_net_premium is 'Total Liability Premium(B)' on Package/Commercial policies; '0' or blank on SAOD policies. "
                "gst is 'Total Tax Payable in ₹'. "
                "net_premium is 'Total Package Premium(A+B)' for Package/Commercial policies, and 'Total Own Damage Premium(A)' for SAOD (pre-tax). "
                "gross_premium is 'Total Premium Payable In ₹' for all variants (post-tax final total). "
                "payment_amount is the same as gross_premium."
            )
        if "payment_mode" in field_keys:
            rules.append(
                "- ICICI Lombard-specific: payment_mode defaults to 'Online' unless an explicit cheque/DD reference number is printed."
            )
        if "bank_name" in field_keys or "cheque_number" in field_keys:
            rules.append(
                "- ICICI Lombard-specific: bank_name and cheque_number should be left blank. "
                "'Hypothecated To' is a financier/lender (e.g. 'ICICI BANK LTD'), NOT a payment bank; never map it to bank_name."
            )
        if "cpa" in field_keys:
            rules.append(
                "- ICICI Lombard-specific: for cpa, check the OWN DAMAGE(A) or LIABILITY(B) table for a row labeled "
                "'PA Cover for Owner Driver' or 'PA Cover for Owner-Driver' with a non-zero premium amount. "
                "If present and non-zero, return 'Yes'; otherwise return 'No'. "
                "Do NOT treat the standard ₹15,00,000 liability disclosure paragraph as evidence of purchase."
            )
        if "insurer" in field_keys:
            rules.append(
                "- ICICI Lombard-specific: insurer is always 'ICICI Lombard General Insurance Company Limited'."
            )
        return "\n".join(rules)

    return ""



def _split_proposer_and_nominee_text(text: str) -> tuple[str, str]:
    raw_text = str(text or "")
    boundary = NOMINEE_BOUNDARY_RE.search(raw_text)
    if not boundary:
        return raw_text, ""
    return raw_text[: boundary.start()], raw_text[boundary.start():]


def _extract_first_labeled_dob(text: str) -> str:
    match = re.search(
        r"(?:date\s*of\s*birth|dob)\s*[:#-]?\s*([0-9*/.-]+)",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    value = match.group(1).strip()
    if re.fullmatch(r"[*/ .-]+", value):
        return ""
    return value


def _is_valid_dob_year(value: str) -> bool:
    normalized = _normalize_date_only_string(value)
    if not normalized:
        return False

    year_match = re.search(r"(?:\d{2}/\d{2}/|^)(\d{4})$", normalized)
    if not year_match:
        return False

    year = int(year_match.group(1))
    current_year = datetime.now().year
    return MIN_VALID_DOB_YEAR <= year <= current_year


def _is_nominee_key(field_name: str) -> bool:
    return "nominee" in _normalize_field_key(field_name)


def _sanitize_export_row_data(row_data: dict) -> dict:
    sanitized: dict = {}
    for key, value in row_data.items():
        if _is_nominee_key(str(key)):
            continue
        sanitized[key] = value

    dob_value = _clean_text_field(sanitized.get("dob"))
    if dob_value and not _is_valid_dob_year(dob_value):
        sanitized["dob"] = ""

    return sanitized


def _field_prompt_line(field_key: str) -> str:
    if field_key in DATE_ONLY_FIELDS:
        return f"  {field_key:<22}: string (date only in dd/mm/yyyy, no time, or null)"
    if field_key == "business_type":
        return f"  {field_key:<22}: string (e.g. Rollover, New Business, Renewal, or null)"
    if field_key == "policy_type":
        return (
            f"  {field_key:<22}: string (extract the COMPLETE, FULL policy type name exactly as written "
            "in the document, preserving all descriptive words like 'Package', 'Comprehensive', "
            "'Long Term', 'Liability Only', etc., e.g. 'Private Car Package Policy', "
            "'Two Wheeler Comprehensive Policy', 'Goods Carrying Comprehensive Policy'. "
            "Do NOT shorten to just 'Private Car' or 'Two Wheeler': keep the entire policy type description)"
        )
    if field_key in ("current_ncb", "previous_ncb"):
        return (
            f"  {field_key:<22}: string (the NCB percentage as a plain number, e.g. '20' for 20%, "
            "do NOT return a rupee amount; if the document shows 'No Claim Bonus: ₹ 0' return '0')"
        )
    if field_key == "cpa":
        return (
            f"  {field_key:<22}: string ('Yes' if any Compulsory Personal Accident cover is present "
            "with a non-zero amount; 'No' if absent or zero. Return ONLY 'Yes' or 'No', never an amount)"
        )
    if field_key == "gst":
        return (
            f"  {field_key:<22}: string (the TOTAL GST amount as a plain number only: "
            "add CGST and SGST together if listed separately, e.g. CGST 977.9 + SGST 977.9 = '1955.80'; "
            "never include labels, symbols, or component breakdown: just the final sum)"
        )
    if field_key == "od_net_premium":
        return (
            f"  {field_key:<22}: string (ONLY the TOTAL own damage premium value "
            "from labels like 'Total own damage premium': do NOT use base/basic/add-on/net(A) values)"
        )
    if field_key == "tp_net_premium":
        return (
            f"  {field_key:<22}: string (ONLY the TOTAL liability premium value "
            "from labels like 'Total liability premium': do NOT use base/basic/PA/net(B) values)"
        )
    if field_key in ("phone_number", "mobile_number"):
        return (
            f"  {field_key:<22}: string (the contact/phone/mobile number: copy EXACTLY as printed in the document. "
            "If it contains masking characters such as 'X', 'x', '*', or '•', preserve them EXACTLY; do NOT unmask, "
            "replace with digits, or omit masking characters; or null)"
        )
    if field_key == "email":
        return (
            f"  {field_key:<22}: string (the insured person's own email address: "
            "NEVER return a corporate insurer support address such as support@hizuno.com, "
            "support@icicilombard.com, or any address from the insurer's boilerplate footer; "
            "if no personal customer email is present, return null)"
        )
    if field_key == "exclusions":
        return f"  {field_key:<22}: string (brief bullet list of key exclusions, or null)"
    if field_key == "needs_review":
        return f"  {field_key:<22}: boolean (true if any critical field is missing or ambiguous)"
    if field_key == "review_reason":
        return f"  {field_key:<22}: string (explain why review is needed, or null)"
    return f"  {field_key:<22}: string"


def _extraction_field_keys(fields: list[tuple[str, str]]) -> list[str]:
    field_keys: list[str] = []
    seen = set()
    for field_key, _ in fields:
        key = str(field_key).strip()
        if not key or key.lower() in {"file_name", "file", "filename"}:
            continue
        if key not in seen:
            field_keys.append(key)
            seen.add(key)
    return field_keys


def make_extraction_prompt(
    policy_text: str,
    fields: list[tuple[str, str]],
    company_mode: str | None = None,
) -> str:
    field_keys = _extraction_field_keys(fields)
    lines = [_field_prompt_line(key) for key in field_keys]
    company_rules = _company_specific_prompt_rules(policy_text, field_keys, company_mode=company_mode)
    return EXTRACTION_PROMPT_TEMPLATE.format(
        fields="\n".join(lines),
        policy_text=policy_text,
        company_rules=company_rules,
    )


def build_json_schema(fields: list[tuple[str, str]]) -> dict:
    """Strict JSON schema matching the fields in the prompt, for use with
    the Responses API's structured-output mode. This guarantees the model
    always returns valid JSON with exactly these keys: no missing keys,
    no malformed output to fall back on."""
    field_keys = _extraction_field_keys(fields)
    properties: dict = {}
    for key in field_keys:
        if key == "needs_review":
            properties[key] = {"type": "boolean"}
        else:
            properties[key] = {"type": ["string", "null"]}
    return {
        "type": "object",
        "properties": properties,
        "required": field_keys,
        "additionalProperties": False,
    }

# The gi policy now sends the full extracted document text without any
# character cut-off. The old quick/full scan limits are no longer used.
QUICK_SCAN_CHARS: int | None = None
FULL_SCAN_CHARS: int | None = None



# ─────────────────────────────────────────────────────────────────────────────
# PDF text extraction
# ─────────────────────────────────────────────────────────────────────────────

def _scrub_go_digit_watermark(text: str) -> str:
    """Remove Go Digit PDF watermark noise from extracted text.

    Go Digit PDFs contain a rotated diagonal watermark (e.g. "SAMPLE NAME
    DL9CAB1234 HYUNDAI XCENT 2026-07-21 2027-07-20 Digit Private Car Policy")
    AND a scattered numeric/alphabetic overlay that PyMuPDF extracts as many
    isolated short tokens on separate lines, e.g.:

        N\n01\nG0\nP\n1M\n1\n61\n4.8\nMA\nfal\n45Br\nIG0A\nST0\nA\n0\n84119\n0\n4906\nD19\n1000\n1

    Strategy:
    1. Remove the diagonal watermark line (insured name + vehicle details
       repeated at the bottom of each page as a long single line).
    2. Collapse runs of short isolated tokens: defined as 6+ consecutive
       lines each containing ≤6 non-space characters and no real word
       (no letters surrounded by vowels → consonants pattern longer than 4).
       This preserves legitimate short fields like "DL9C", "5", "NA".
    """
    lines = text.split("\n")
    cleaned: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Remove diagonal watermark footers: long lines with vehicle identifiers
        # printed at the very bottom of each Go Digit page.
        # Pattern: "NAME REGNUM MAKE MODEL DATE DATE PolicyProductName"
        if _GODIGIT_WATERMARK_FOOTER_RE.match(stripped):
            i += 1
            continue

        # Detect a run of watermark noise tokens: 6+ consecutive lines that
        # are all <=6 chars and lack any real word.
        if stripped and len(stripped) <= 6 and not _REAL_WORD_RE.search(stripped):
            run_lines = [stripped]
            j = i + 1
            while j < len(lines):
                next_stripped = lines[j].strip()
                if next_stripped and len(next_stripped) <= 6 and not _REAL_WORD_RE.search(next_stripped):
                    run_lines.append(next_stripped)
                    j += 1
                else:
                    break
            if len(run_lines) >= 6:
                # This is a watermark noise run, drop it entirely
                i = j
                continue
            # Short run (< 6 tokens): keep it; could be real data
        cleaned.append(line)
        i += 1

    return "\n".join(cleaned)


def extract_pdf_text(pdf_bytes: bytes, max_pages: int | None = None) -> tuple[str, int, list[str]]:
    """
    Returns (full_text, page_count, warnings).
    Uses PyMuPDF (fitz) with automatic RapidOCR fallback for pages lacking digital text.
    """
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        try:
            import fitz  # type: ignore
        except ImportError:
            return "", 0, ["PyMuPDF is not installed. Run: pip install pymupdf"]

    warnings: list[str] = []
    pages: list[str] = []
    total_pages = 0

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        total_pages = doc.page_count
        page_limit = total_pages if max_pages is None else min(max_pages, total_pages)
        blank_pages = 0

        for i in range(page_limit):
            try:
                page = doc.load_page(i)
                blocks = page.get_text("blocks")
                if blocks:
                    sorted_blocks = sorted(blocks, key=lambda b: (round(b[1] / 8) * 8, b[0]))
                    text = "\n".join(b[4].strip() for b in sorted_blocks if b[4].strip())
                else:
                    text = str(page.get_text("text") or "").strip()
                if text:
                    pages.append(f"[[PAGE {i + 1} START]]\n{text}\n[[PAGE {i + 1} END]]")
                else:
                    # Fallback: rasterize page and apply RapidOCR
                    ocr_text = ""
                    try:
                        import cv2
                        import numpy as np
                        from rapidocr_onnxruntime import RapidOCR
                        pix = page.get_pixmap(dpi=150)
                        img_array = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, pix.n))
                        if pix.n == 4:
                            img_array = cv2.cvtColor(img_array, cv2.COLOR_RGBA2RGB)
                        engine = RapidOCR()
                        ocr_res, _ = engine(img_array)
                        if ocr_res:
                            ocr_text = "\n".join([line[1] for line in ocr_res if line and len(line) > 1])
                    except Exception:
                        pass

                    if ocr_text.strip():
                        pages.append(f"[[PAGE {i + 1} START (OCR)]]\n{ocr_text.strip()}\n[[PAGE {i + 1} END]]")
                    else:
                        blank_pages += 1
            except Exception as page_exc:
                blank_pages += 1
                warnings.append(f"Page {i + 1} read note: {page_exc}")

        doc.close()
    except Exception as exc:
        return "", 0, [f"Could not open or parse PDF: {exc}"]

    if blank_pages and not pages:
        warnings.append("No text could be extracted from this PDF.")

    full_text = "\n\n".join(pages)

    # Go Digit PDFs embed a visual watermark whose fragments are extracted as
    # isolated 1–6-character tokens interspersed in the text (e.g. "N\n01\nG0\n
    # P\n1M\n1\n61\n4.8\nMA\nfal\n45Br\n...").  These corrupt field boundaries
    # and confuse the extractor.  We remove them here so downstream parsing
    # works on clean text.
    if _is_go_digit_policy_document(full_text):
        full_text = _scrub_go_digit_watermark(full_text)

    return full_text, total_pages, warnings


# ─────────────────────────────────────────────────────────────────────────────
# Inference
# ─────────────────────────────────────────────────────────────────────────────

def _parse_model_json(raw_output: str) -> dict:
    """Strip markdown fences and parse the model's JSON result with truncated JSON repair."""
    cleaned = re.sub(r"^```(?:json)?\s*", "", str(raw_output or "").strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        # Resilient repair for JSON truncated by token limits
        if "{" in cleaned:
            start_idx = cleaned.find("{")
            candidate = cleaned[start_idx:]
            # Strip trailing incomplete key or unclosed string
            repaired = re.sub(r',\s*"[^"]*"\s*:\s*"?[^"]*$', '', candidate)
            repaired = re.sub(r',\s*"[^"]*"\s*$', '', repaired)
            repaired = repaired.rstrip(", \n\r\t")
            if repaired.count('"') % 2 != 0:
                repaired += '"'
            repaired += "}"
            try:
                parsed = json.loads(repaired)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass

        return {
            "error": "Model output was not valid JSON.",
            "raw_output": raw_output,
        }

    return {
        "error": "Model output was valid JSON but not an object.",
        "raw_output": raw_output,
    }


# Corporate / insurer boilerplate email domains that should never be returned
# as the customer's email address.
_BOILERPLATE_EMAIL_DOMAINS: set[str] = {
    "hizuno.com", "zuno.com", "edelweissinsurance.com",
    "icicilombard.com", "hdfcergo.com", "bajajallianz.com",
    "reliancegeneral.co.in", "tataaig.com", "sbigeneral.in",
    "niacl.org.in", "orientalinsurance.org.in", "uiic.co.in",
    "newindia.co.in", "starhealth.in", "manipalcigna.com",
    "adityabirlacapital.com", "godigit.com", "acko.com",
}

_CUSTOMER_EMAIL_CONTEXT_RE = re.compile(
    r"(?:email\s*id|email\s*address|email)\s*[:#-]?\s*([\w.+-]+@[\w-]+\.[\w.]+)",
    flags=re.IGNORECASE,
)
_ANY_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def _extract_customer_email(text: str, company_mode: str | None = None) -> str:
    """Return the customer's email address.

    Strategy:
    1. Look for an email that immediately follows a customer-context label
       (e.g. "Email ID:", "Email Address:"). This is the most reliable signal.
    2. Fall back to the first bare email found anywhere, but skip addresses
       from known corporate/boilerplate domains so we don't return
       support@hizuno.com or similar footer addresses.
    """
    source_text = str(text or "")
    is_zuno = company_mode == "ZUNO" or (
        company_mode is None and _is_zuno_policy_document(source_text)
    )
    if is_zuno:
        zuno_proposer_text = _extract_zuno_proposer_details_section(source_text)
        if zuno_proposer_text.strip():
            source_text = zuno_proposer_text

    # Preferred: labelled context
    m = _CUSTOMER_EMAIL_CONTEXT_RE.search(source_text)
    if m:
        addr = m.group(1).strip()
        domain = addr.split("@")[-1].lower()
        if domain not in _BOILERPLATE_EMAIL_DOMAINS:
            return addr

    # Fallback: first non-boilerplate email in the document
    for m in _ANY_EMAIL_RE.finditer(source_text):
        addr = m.group(0).strip()
        domain = addr.split("@")[-1].lower()
        if domain not in _BOILERPLATE_EMAIL_DOMAINS:
            return addr

    return ""


def _extract_proposer_dob(text: str, company_mode: str | None = None) -> str:
    """Return the proposer/insured's date of birth, not the nominee's.

    Zuno-style documents contain a nominee section that includes its own
    'Date of Birth' line. We split the text at the nominee section boundary
    and search only in the proposer portion.  If the DOB is masked (***) we
    return empty string rather than the mask.
    """
    is_zuno = company_mode == "ZUNO" or (
        company_mode is None and _is_zuno_policy_document(text)
    )
    if is_zuno:
        zuno_proposer_text = _extract_zuno_proposer_details_section(text)
        if zuno_proposer_text.strip():
            return _extract_first_labeled_dob(zuno_proposer_text)

    proposer_text, _ = _split_proposer_and_nominee_text(text)
    return _extract_first_labeled_dob(proposer_text)


def _extract_make_model(text: str) -> str:
    """Extract the full Make + Model string.

    Zuno policies present Make and Model as separate adjacent table columns,
    so the plain-text extraction yields lines like:
        MARUTI  WAGON\\nR 1.0  VXI CNG  998.00 ...
    A simple 'make' + 'model' concatenation only picks up MARUTI and whatever
    the regex for 'model' matches, losing the rest.

    Strategy:
    1. Try 'Vehicle Model and Variant' (proposal transcript) which gives the
       full string in one field.
    2. Try 'Make … Model' in the same table row region.
    3. Fall back to separate make / model regex and concatenate.
    """
    # Proposal transcript: "Vehicle Model and Variant  WAGON R 1.0 & VXI CNG"
    m = re.search(
        r"Vehicle\s+Model\s+and\s+Variant\s*[:#-]?\s*([A-Za-z0-9 .&/,'-]+)",
        text, flags=re.IGNORECASE,
    )
    if m:
        # Strip the variant part (after '&') so we only keep Make+Model
        value = m.group(1).strip()
        if "&" in value:
            value = value.split("&")[0].strip()
        return value

    # Table row: look for Make then Model within a short window
    mm = re.search(
        r"\bMake\b.{0,60}?\bModel\b",
        text, flags=re.IGNORECASE | re.DOTALL,
    )
    if mm:
        region = text[mm.start(): mm.start() + 200]
        mk = re.search(r"Make\s*[:#-]?\s*([A-Za-z]+)", region, re.IGNORECASE)
        mo = re.search(r"Model\s*[:#-]?\s*([A-Za-z0-9 .,'-]+)", region, re.IGNORECASE)
        if mk and mo:
            make_val = mk.group(1).strip()
            model_val = mo.group(1).strip()
            return f"{make_val} {model_val}".strip()

    # Last resort: separate patterns
    mk2 = re.search(r"make\s*[:#-]?\s*([A-Za-z0-9 .,'-]+)", text, re.IGNORECASE)
    mo2 = re.search(r"model\s*[:#-]?\s*([A-Za-z0-9 .,'-]+)", text, re.IGNORECASE)
    make_val = mk2.group(1).strip() if mk2 else ""
    model_val = mo2.group(1).strip() if mo2 else ""
    return " ".join(p for p in [make_val, model_val] if p)


def _extract_full_variant(text: str) -> str:
    """Extract the variant including fuel-type suffix (e.g. 'VXI CNG').

    In tabular Zuno PDFs, 'Variant' and the fuel column (CNG / LPG / EV) are
    adjacent cells; plain text extraction often puts them on the same line or
    on consecutive short lines.  We capture both and combine them when the
    variant doesn't already contain the fuel descriptor.
    """
    m = re.search(r"variant\s*[:#-]?\s*([A-Za-z0-9 .,'-]+)", text, flags=re.IGNORECASE)
    if not m:
        return ""
    variant = m.group(1).strip()

    # Check whether a CNG / LPG / EV / Hybrid qualifier immediately follows
    # the variant value (within ~30 chars) and append if missing.
    fuel_qualifiers = ("CNG", "LPG", "EV", "ELECTRIC", "HYBRID", "PETROL", "DIESEL")
    variant_upper = variant.upper()
    already_has_fuel = any(q in variant_upper for q in fuel_qualifiers)

    if not already_has_fuel:
        # Look within 40 chars after the variant match for a fuel qualifier
        tail = text[m.end(): m.end() + 40]
        for q in fuel_qualifiers:
            if re.search(rf"\b{q}\b", tail, flags=re.IGNORECASE):
                variant = f"{variant} {q.capitalize() if q not in ('CNG','LPG','EV') else q}"
                break

    return variant


def _extract_city_state_from_address(address_text: str, full_text: str = "") -> tuple[str, str]:
    """Split city/state from a free-form address block.

    Priority:
    1) explicit city+pin,state pattern (e.g. "Surat 394230, Gujarat")
    2) "<City/RTO>-<State>" combined tail token (common in TATA AIG addresses,
       e.g. "BALLABGARH-HARYANA, 121004" or "DARPAN COLONY-MADHYA PRADESH, 474011")
    3) comma/newline tokenization of address tail
    """
    address = str(address_text or "").strip()
    text = str(full_text or "")
    if not address and not text:
        return "", ""

    explicit = re.search(
        r"\b([A-Za-z][A-Za-z .'-]{2,})\s+\d{6}\s*,\s*([A-Za-z][A-Za-z .'-]{2,})\b",
        address or text,
        flags=re.IGNORECASE,
    )
    if explicit:
        return explicit.group(1).strip(), explicit.group(2).strip()

    tokens = [
        token.strip(" ,")
        for token in re.split(r"[\n,]+", address)
        if token and token.strip(" ,")
    ]
    # Drop a trailing pure-pincode token so it doesn't get mistaken for the
    # city/state token (e.g. ["...", "GURGAON-HARYANA", "122009"]).
    if tokens and re.fullmatch(r"\d{6}", tokens[-1]):
        tokens = tokens[:-1]
    if not tokens:
        return "", ""

    state = ""
    city = ""

    last_token = tokens[-1]

    # Case: combined "<City/RTO name>-<State>" tail, e.g. "BALLABGARH-HARYANA"
    # or "DARPAN COLONY-MADHYA PRADESH" or "NOIDA HO-UTTAR PRADESH". Only
    # split on the hyphen when the right-hand side is a recognisable Indian
    # state/UT name -- otherwise a hyphenated street name could be
    # misinterpreted.
    if "-" in last_token:
        left, _, right = last_token.rpartition("-")
        right_clean = re.sub(r"\bIndia\b", "", right, flags=re.IGNORECASE).strip(" ,")
        matched_state = _match_indian_state(right_clean)
        if left.strip() and matched_state:
            # Strip common RTO/postal suffixes (e.g. "NOIDA HO" -> "NOIDA").
            left_clean = re.sub(
                r"\b(?:HO|H\.O\.?|SO|S\.O\.?|RTO|R\.T\.O\.?|GPO|G\.P\.O\.?)\b\s*$",
                "",
                left.strip(),
                flags=re.IGNORECASE,
            ).strip(" ,-")
            city = left_clean or left.strip()
            state = matched_state
            return city, state

    if re.search(r"[A-Za-z]", last_token):
        state = re.sub(r"\bIndia\b", "", last_token, flags=re.IGNORECASE).strip(" ,")

    if len(tokens) >= 2:
        city_candidate = tokens[-2]
        city_candidate = re.sub(r"\b\d{6}\b", "", city_candidate).strip(" ,")
        city_candidate = re.sub(r"\bIndia\b", "", city_candidate, flags=re.IGNORECASE).strip(" ,")
        if re.search(r"[A-Za-z]", city_candidate):
            city = city_candidate

    return city, state


# Canonical Indian state / union-territory names, used to validate the
# right-hand side of a hyphenated "<City>-<State>" address tail before
# splitting on it (see _extract_city_state_from_address above).
_INDIAN_STATES_UTS = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
    "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim",
    "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand",
    "West Bengal",
    "Andaman and Nicobar Islands", "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Jammu and Kashmir",
    "Ladakh", "Lakshadweep", "Puducherry", "NCT of Delhi", "New Delhi",
]
_INDIAN_STATES_LOOKUP = {s.upper(): s for s in _INDIAN_STATES_UTS}
_INDIAN_STATES_LOOKUP["NCT OF DELHI"] = "Delhi"
_INDIAN_STATES_LOOKUP["NEW DELHI"] = "Delhi"


def _match_indian_state(candidate: str) -> str:
    """Return the canonical state/UT name if `candidate` names one, else ""."""
    key = re.sub(r"\s+", " ", str(candidate or "")).strip().upper()
    return _INDIAN_STATES_LOOKUP.get(key, "")


def _extract_tata_od_tp_premium(text: str) -> tuple[str, str]:
    """
    TATA AIG-specific: compute OD_Premium and TP_Premium from the Schedule of
    Premium components.

    OD_Premium = "Total Own Damage Premium" + "Total Add on Premium" + "Total Others"
    TP_Premium = "Total Liability Premium" (+ a "Total ... (E)" row if present)

    IMPORTANT: the row LABEL (letter in parentheses) is matched by text/role, not by a
    fixed letter, because it differs between policy layouts:
      - Package policies (OD+TP): A=Own Damage, C=Add-on, B=Liability, D=Others
      - Standalone OD (SAOD) policies (OD only, no TP section): A=Own Damage,
        B=Add-on, C=Others, and there is no Liability row at all, so
        tp_net_premium comes back empty for these.
    Matching by role keeps both layouts correct without hardcoding letters.

    Returns (od_net_premium, tp_net_premium) as plain numeric strings
    (empty string if the base "Own Damage" / "Liability" row could not be found).
    """
    raw = str(text or "")

    def _amount(pattern: str) -> float | None:
        m = re.search(pattern, raw, re.IGNORECASE)
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None

    a_val = _amount(r"Total\s+Own\s+Damage\s+Premium\s*\([A-Za-z]\)\s*\n?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)")
    addon_val = _amount(r"Total\s+Add[\s-]?on\s+Premium\s*\([A-Za-z]\)\s*\n?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)")
    others_val = _amount(r"Total\s+Others\s*\([A-Za-z]\)\s*\n?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)")
    liability_val = _amount(r"Total\s+Liability\s+Premium\s*\([A-Za-z]\)\s*\n?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)")
    # E only appears on some policies: generic "Total ... (E)" row, added to TP.
    e_val = _amount(r"Total\s+[A-Za-z /]+\(E\)\s*\n?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)")

    def _fmt(value: float) -> str:
        return str(int(value)) if value == int(value) else f"{value:.2f}"

    if a_val is None:
        od_net_premium = ""
    else:
        od_net_premium = _fmt(a_val + (addon_val or 0) + (others_val or 0))

    if liability_val is None:
        if "standalone" in raw.lower() or "saod" in raw.lower():
            tp_net_premium = "0.00"
        else:
            tp_net_premium = ""
    else:
        tp_net_premium = _fmt(liability_val + e_val) if e_val is not None else _fmt(liability_val)

    return od_net_premium, tp_net_premium


def _format_tata_policy_number(raw_policy_number: str) -> str:
    """Format a TATA AIG policy number from its raw 3-part form.

    TATA AIG policy numbers are printed as three space-separated parts:
        <10-digit base>  <2-digit renewal count>  <2-digit suffix (ignored)>
    e.g. "1234567890 03 00"  or  "12345678900300" (all digits, no spaces)

    Formatting rules:
        renewal == 00  ->  "1234567890"        (New Business: no suffix)
        renewal == 01  →  "1234567890/R"      (first renewal)
        renewal >= 02  →  "1234567890-02"     (second renewal onwards)

    If the input does not look like a 14-digit TATA policy number, it is
    returned unchanged so other insurers' numbers are not affected.
    """
    if not raw_policy_number:
        return raw_policy_number
    digits_only = re.sub(r"\s+", "", str(raw_policy_number))
    if not digits_only.isdigit() or len(digits_only) != 14:
        # Not a 14-digit TATA policy number: return as-is
        return raw_policy_number
    base_pol    = digits_only[:10]   # e.g. "1234567890"
    renewal_str = digits_only[10:12] # e.g. "03"
    try:
        renewal_count = int(renewal_str)
    except ValueError:
        return raw_policy_number
    if renewal_count == 0:
        return base_pol
    elif renewal_count == 1:
        return f"{base_pol}/R"
    else:
        return f"{base_pol}-{renewal_count:02d}"


def _extract_tata_fields(text: str) -> dict:
    raw = str(text or "")

    # Customer name
    cust_m = re.search(r"(?m)^Name\s*\n\s*([A-Za-z][A-Za-z .'-]+)", raw)
    if not cust_m:
        cust_m = re.search(r"Insured\s+Details[^\n]*\n[\s\S]*?(?:^|\n)Name\s*\n?\s*([A-Za-z][A-Za-z .'-]+)", raw, re.IGNORECASE)
    if not cust_m:
        cust_m = re.search(r"1\.\s+Name\s*(?:\(Registered[^)]*\))?\s*[:#-]?\s*([A-Za-z][A-Za-z .'-]+)", raw, re.IGNORECASE)
    customer_name = cust_m.group(1).strip() if cust_m else ""
    customer_name = re.sub(r"\s+(?:Address|Contact|Email|GSTIN|Policy|Period).*", "", customer_name, flags=re.IGNORECASE).strip()

    # Address
    addr_m = re.search(r"Address\s*\n\s*([\s\S]*?)\s*\n\s*Contact\s+No\.", raw, re.IGNORECASE)
    if not addr_m:
        addr_m = re.search(r"2\.\s+Address\s+for\s+Communication\s*:\s*([^\n]+(?:\n[^\n]+)?)", raw, re.IGNORECASE)
    address = addr_m.group(1).strip().replace("\n", ", ") if addr_m else ""
    address = re.sub(r"\s*,\s*", ", ", address).strip(" ,")

    # PIN, City, State
    pin_m = re.search(r"\b(\d{6})\b", address)
    pin = pin_m.group(1) if pin_m else ""
    city, state = _extract_city_state_from_address(address, raw)

    # Phone / Mobile
    contact_m = re.search(r"Contact\s+No\.?\s*\n?\s*([+0-9*X -]{7,20})", raw, re.IGNORECASE)
    contact_no = contact_m.group(1).strip() if contact_m else ""
    phone_number = contact_no
    mobile_number = contact_no

    # Email
    email_m = re.search(r"Email\s+ID\s*\n?\s*([A-Za-z0-9*._%+-]+@[A-Za-z0-9*.-]+\.[A-Za-z]{2,})", raw, re.IGNORECASE)
    email = email_m.group(1).strip() if email_m else ""

    # Policy Number
    # TATA AIG policy numbers are printed as three space-separated parts:
    #   <10-digit base>  <2-digit renewal count>  <2-digit suffix (ignored)>
    # e.g. "1234567890 03 00"
    # Formatting rules:
    #   renewal == 00  →  "1234567890"        (New Business, no suffix)
    #   renewal == 01  →  "1234567890/R"      (first renewal)
    #   renewal >= 02  →  "1234567890-02"     (second renewal onwards)
    pol_m = re.search(r"Policy\s+No\.?\s*\n?\s*([0-9][0-9 ]{7,25})", raw, re.IGNORECASE)
    if not pol_m:
        pol_m = re.search(r"Your\s+Policy\s+Number\s+is\s+([0-9][0-9 ]{7,25})", raw, re.IGNORECASE)
    if pol_m:
        raw_pol = re.sub(r"\s+", "", pol_m.group(1).strip())  # e.g. "12345678900300"
        policy_number = _format_tata_policy_number(raw_pol)
    else:
        policy_number = ""

    # Policy Type
    pt_m = re.search(r"(Auto\s+Secure\s*[-–]\s*[A-Za-z ]+(?:Policy|Cover))", raw, re.IGNORECASE)
    raw_pt = pt_m.group(1).strip() if pt_m else "Auto Secure - Policy"
    policy_type = re.sub(r"^[^-–]+[-–]\s*", "", raw_pt).strip()

    # Policy Start and End
    start_m = re.search(r"(?:Own\s+Damage\s+Cover|Third-Party\s+Cover|TP\s+Cover)\s*\n\s*([0-9]{2}/[0-9]{2}/[0-9]{4})", raw, re.IGNORECASE)
    if not start_m:
        start_m = re.search(r"[•\s]*(?:OD|TP)\s+Cover\s+Period\s*:\s*([0-9]{2}/[0-9]{2}/[0-9]{4})", raw, re.IGNORECASE)
    policy_start = start_m.group(1).strip() if start_m else ""

    end_m = re.search(r"(?:Own\s+Damage\s+Cover|Third-Party\s+Cover|TP\s+Cover)\s*\n\s*[0-9]{2}/[0-9]{2}/[0-9]{4}\s*(?:\([^)]*\))?\s*\n\s*([0-9]{2}/[0-9]{2}/[0-9]{4})", raw, re.IGNORECASE)
    if not end_m:
        end_m = re.search(r"to\s*\n?\s*([0-9]{2}/[0-9]{2}/[0-9]{4})\s*\(Midnight\)", raw, re.IGNORECASE)
    if not end_m:
        end_m = re.search(r"to\s+([0-9]{2}/[0-9]{2}/[0-9]{4})\s*\(Midnight\)", raw, re.IGNORECASE)
    policy_end = end_m.group(1).strip() if end_m else ""

    # Issue Date / Txn Date
    rcpt_date_m = re.search(r"Receipt\s+Date\s*\n?\s*([0-9]{2}/[0-9]{2}/[0-9]{4})", raw, re.IGNORECASE)
    policy_issue_date = rcpt_date_m.group(1).strip() if rcpt_date_m else ""
    txn_date = policy_issue_date

    # Vehicle Number
    veh_m = re.search(r"Registration\s+No\.?\s*\n\s*([A-Za-z0-9 -]{5,20})", raw, re.IGNORECASE)
    vehicle_number = _normalize_vehicle_number(veh_m.group(1).strip()) if veh_m else ""

    # Make & Model / Variant
    make = ""
    model_variant = ""
    # Try transcript first (cleaner)
    trans_veh_m = re.search(r"3\.\s+Vehicle\s+Details\s*:\s*([^\n]+)", raw, re.IGNORECASE)
    if trans_veh_m:
        parts = [p.strip() for p in trans_veh_m.group(1).split("/") if p.strip()]
        if len(parts) >= 1:
            make = parts[0]
        if len(parts) >= 2:
            model_variant = " / ".join(parts[1:])
    else:
        mmv_m = re.search(r"Make\s*/\s*Model\s*/\s*Variant\s*\n\s*([\s\S]*?)\s*\n\s*Fuel\s+Type", raw, re.IGNORECASE)
        if mmv_m:
            joined = re.sub(r"\s+", " ", mmv_m.group(1).strip())
            parts = [p.strip() for p in joined.split("/") if p.strip()]
            if len(parts) >= 1:
                make = parts[0]
            if len(parts) >= 2:
                model_variant = " / ".join(parts[1:])

    # Fuel Type
    fuel_m = re.search(r"Fuel\s+Type\s*\n\s*([A-Za-z/]+)", raw, re.IGNORECASE)
    fuel_type = fuel_m.group(1).strip() if fuel_m else ""

    # Engine Number
    eng_m = re.search(r"Engine\s+Number\s*(?:/\s*Motor\s+No\.[^\n]*)?\s*\n\s*([A-Za-z0-9]+)", raw, re.IGNORECASE)
    engine_number = eng_m.group(1).strip() if eng_m else ""

    # Chassis Number
    chassis_m = re.search(r"Chassis\s+No\.?\s*\n\s*([A-Za-z0-9]+)", raw, re.IGNORECASE)
    chassis_number = chassis_m.group(1).strip() if chassis_m else ""

    # CC/KW
    cc_m = re.search(r"CC\s*/\s*KW\s*\n\s*([0-9]+)", raw, re.IGNORECASE)
    cc_kwh = cc_m.group(1).strip() if cc_m else ""

    # Year of Manufacture
    mfg_m = re.search(r"Mfg\.?\s*Year\s*\n\s*([0-9]{4})", raw, re.IGNORECASE)
    year_of_manufacture = mfg_m.group(1).strip() if mfg_m else ""

    # Seating Capacity / Passenger
    seat_m = re.search(r"Seating\s+Capacity\s*(?:\(Including\s+Driver\))?\s*\n\s*([0-9]+)", raw, re.IGNORECASE)
    passenger = seat_m.group(1).strip() if seat_m else ""

    # RTO Location
    rto_m = re.search(r"RTO\s+Location\s*\n\s*([A-Za-z0-9 .,'-]+)", raw, re.IGNORECASE)
    rto_name = rto_m.group(1).strip() if rto_m else ""

    # IDV
    idv_m = re.search(r"Total\s*\n\s*IDV\s*\n\s*\(₹\)\s*\n\s*(?:1\s*\n\s*)?([0-9]+)", raw, re.IGNORECASE)
    if not idv_m:
        idv_m = re.search(r"Total\s+IDV[^\n]*\n[^\n]*\n?\s*([0-9]{5,10})", raw, re.IGNORECASE)
    idv = idv_m.group(1).strip() if idv_m else ""

    # OD and TP Net Premium
    od_net_premium, tp_net_premium = _extract_tata_od_tp_premium(raw)

    # Current NCB
    ncb_m = re.search(r"Less\s*:\s*No\s+claim\s+bonus\s*\(\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*\)", raw, re.IGNORECASE)
    if not ncb_m:
        ncb_m = re.search(r"NCB\s+Claimed\s*:\s*([0-9]+)\s*%", raw, re.IGNORECASE)
    current_ncb = ncb_m.group(1).strip() if ncb_m else "0"

    # Previous NCB
    prev_ncb_m = re.search(r"NCB\s+in\s+Previous\s+Policy\s*:\s*([0-9]+)\s*%", raw, re.IGNORECASE)
    previous_ncb = prev_ncb_m.group(1).strip() if prev_ncb_m else "0"

    # CPA
    cpa = "No"
    p1_cpa_m = re.search(r"Compulsory\s+PA\s+Cover\s+for\s+Owner-Driver\s*\n\s*([^\n]+)", raw, re.IGNORECASE)
    if p1_cpa_m:
        p1_val = p1_cpa_m.group(1).strip()
        if re.search(r"\d{2}/\d{2}/\d{4}", p1_val) and "NA" not in p1_val.upper():
            cpa = "Yes"
        else:
            cpa = "No"
    else:
        cpa_m = re.search(r"Compulsory\s+PA\s+(?:cover|Cover)\s+for\s+Owner[\s-]Driv(?:e|er)[^\n]*?[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)", raw, re.IGNORECASE)
        if cpa_m:
            try:
                cpa = "Yes" if float(cpa_m.group(1).replace(",", "")) > 0 else "No"
            except ValueError:
                cpa = "No"
        else:
            prop_cpa_m = re.search(r"•CPA\s+Cover\s+Period\s*:\s*([^\n]+)", raw, re.IGNORECASE)
            if prop_cpa_m:
                pval = prop_cpa_m.group(1).strip().upper()
                if "NA TO NA" in pval or pval == "NA":
                    cpa = "No"
                elif re.search(r"\d{2}/\d{2}/\d{4}", pval):
                    cpa = "Yes"

    # GST
    gst = ""
    igst_m = re.search(r"IGST\s*@\s*[0-9]+%?\s*\n?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)", raw, re.IGNORECASE)
    if igst_m:
        try:
            gst = f"{float(igst_m.group(1).replace(',', '')):.2f}"
        except ValueError:
            gst = igst_m.group(1).replace(",", "")
    else:
        cgst_m = re.search(r"CGST\s*(?:/UGST)?\s*@\s*[0-9]+%?\s*\n?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)", raw, re.IGNORECASE)
        sgst_m = re.search(r"SGST\s*(?:/UGST)?\s*@\s*[0-9]+%?\s*\n?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)", raw, re.IGNORECASE)
        if cgst_m and sgst_m:
            try:
                gst = f"{float(cgst_m.group(1).replace(',','')) + float(sgst_m.group(1).replace(',','')):.2f}"
            except ValueError:
                pass

    # Payment Mode & Payment Amount from Receipt table
    payment_mode = "Online"
    rcpt_m = re.search(r"Receipt\s*Receipt\s+No\.[\s\S]*?(?:Notes:|\Z)", raw, re.IGNORECASE)
    if not rcpt_m:
        rcpt_m = re.search(r"Mode\s+of\s+Payment[\s\S]*?(?:Notes:|\Z)", raw, re.IGNORECASE)
    rcpt_text = rcpt_m.group(0) if rcpt_m else raw

    row_m = re.search(
        r"(?:^|\n)\s*1\.\s*\n\s*\d{8,12}\s*(?:\n\s*\d{2}\s+\d{2})?\s*\n\s*([A-Za-z][A-Za-z0-9 _\n-]+?)\s*\n\s*[0-9,]+(?:\.[0-9]{1,2})?\b",
        rcpt_text,
        re.MULTILINE,
    )
    if row_m:
        raw_mode = " ".join(row_m.group(1).split()).strip()
        lowered_mode = raw_mode.lower()
        if any(k in lowered_mode for k in ["online", "paymentlink", "upi", "neft", "imps"]):
            payment_mode = "Online"
        elif "cheque" in lowered_mode:
            payment_mode = "Cheque"
        else:
            payment_mode = raw_mode.title()
    else:
        mode_m = re.search(r"Mode\s+of\s+Payment\s*\n?\s*([^\n]+)", raw, re.IGNORECASE)
        if mode_m:
            raw_mode = mode_m.group(1).strip()
            lowered_mode = raw_mode.lower()
            if any(k in lowered_mode for k in ["online", "paymentlink", "upi", "neft", "imps"]):
                payment_mode = "Online"
            elif "cheque" in lowered_mode:
                payment_mode = "Cheque"
            else:
                payment_mode = raw_mode.title()

    # Payment Amount
    prem_m = re.search(r"Total\s+Policy\s+Premium\s*\n?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)", raw, re.IGNORECASE)
    if not prem_m:
        prem_m = re.search(r"Premium\s+Amount\s*\(Including\s+GST\)\s*\n?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)", raw, re.IGNORECASE)
    payment_amount = prem_m.group(1).replace(",", "") if prem_m else ""

    # Business Type
    business_type = _determine_tata_business_type(raw)

    return {
        "policy_number":       policy_number,
        "customer_name":       customer_name,
        "address":             address,
        "city":                city,
        "state":               state,
        "pin":                 pin,
        "phone_number":        phone_number,
        "mobile_number":       mobile_number,
        "dob":                 "",
        "email":               email,
        "insurer":             "TATA AIG General Insurance Company Limited",
        "business_type":       business_type,
        "policy_type":         policy_type,
        "vehicle_number":      vehicle_number,
        "make":                make,
        "model_variant":       model_variant,
        "year_of_manufacture": year_of_manufacture,
        "chassis_number":      chassis_number,
        "engine_number":       engine_number,
        "cc_kwh":              cc_kwh,
        "fuel_type":           fuel_type,
        "rto_name":            rto_name,
        "current_ncb":         current_ncb,
        "previous_ncb":        previous_ncb,
        "passenger":           passenger,
        "gvw":                 "",
        "policy_issue_date":   policy_issue_date,
        "policy_start":        policy_start,
        "policy_end":          policy_end,
        "txn_date":            txn_date,
        "idv":                 idv,
        "od_net_premium":      od_net_premium,
        "tp_net_premium":      tp_net_premium,
        "gst":                 gst,
        "gross_premium":       payment_amount,
        "net_premium":         f"{float(od_net_premium) + float(tp_net_premium):.2f}".rstrip("0").rstrip(".") if od_net_premium and tp_net_premium else "",
        "cpa":                 cpa,
        "payment_mode":        payment_mode,
        "cheque_number":       "",
        "bank_name":           "",
        "payment_amount":      payment_amount,
        "needs_review":        False,
        "review_reason":       None,
    }


def _extract_icici_fields(policy_text: str) -> dict:
    """ICICI Lombard-specific deterministic field extractor.

    Handles all ICICI Lombard motor policy types:
    - Private Car Package Policy
    - Stand-Alone Own Damage Private Car Insurance Policy (SAOD)
    - Two Wheeler Vehicles Package Policy / Stand-Alone Own Damage Two wheeler
    - Miscellaneous Vehicles Package Policy (Commercial)
    """
    raw = str(policy_text or "")

    def _cnum(val: str | None) -> str:
        if not val:
            return ""
        return val.replace(",", "").strip()

    # ── Insurer ────────────────────────────────────────────────────────────
    insurer = "ICICI Lombard General Insurance Company Limited"

    # ── Policy Type ────────────────────────────────────────────────────────
    pt = ""
    pt_m = re.search(
        r"(Private Car Package Policy|Stand-Alone Own Damage Private Car Insurance Policy|"
        r"Miscellaneous Vehicles Package Policy|Two Wheeler Vehicles Package Policy|"
        r"Stand-Alone Own Damage Two wheeler Insurance Policy|Two Wheeler Package Policy)",
        raw,
        re.IGNORECASE,
    )
    if pt_m:
        pt = pt_m.group(1).strip()
    else:
        prod_m = re.search(r"Product\s+Code\s*[:\s]+(300[158](?:/[A-Z])?)", raw, re.IGNORECASE)
        if prod_m:
            code = prod_m.group(1).upper()
            if code == "3001":
                pt = "Private Car Package Policy"
            elif code == "3001/O":
                pt = "Stand-Alone Own Damage Private Car Insurance Policy"
            elif code == "3005":
                pt = "Two Wheeler Vehicles Package Policy"
            elif code == "3005/O":
                pt = "Stand-Alone Own Damage Two wheeler Insurance Policy"
            elif code == "3008":
                pt = "Miscellaneous Vehicles Package Policy"

    # ── Policy Number ──────────────────────────────────────────────────────
    pol_m = re.search(r"\b(300[158](?:/[A-Z])?/\d+/\d+/[A-Za-z0-9]+)\b", raw)
    policy_number = pol_m.group(1) if pol_m else ""

    is_saod = (
        "stand-alone" in pt.lower()
        or "saod" in pt.lower()
        or "/o/" in policy_number.lower()
        or re.search(r"Product\s+Code\s*[:\s]+300[15]/O\b", raw, re.IGNORECASE) is not None
    )

    # ── Business Type ──────────────────────────────────────────────────────
    business_type = _determine_icici_business_type(raw)

    # ── Customer / Insured Name ────────────────────────────────────────────
    name_m = re.search(r"(?:Name\s+of\s+the\s+Insured|Insured\s+Name|Name\s+of\s+Insured)\s*[:\n]+\s*([^\n]+)", raw, re.IGNORECASE)
    customer_name = ""
    if name_m:
        candidate_name = name_m.group(1).strip()
        if not re.match(r"^(?:Period|Vehicle|Policy|Address|Date|Mobile|Ref)\b", candidate_name, re.IGNORECASE):
            customer_name = _strip_trailing_id_suffix(candidate_name)

    # ── Address, City, State, Pin ──────────────────────────────────────────
    address = ""
    city = ""
    state = ""
    pin = ""

    addr_m = re.search(
        r"Address\s*:\s*([^\n]+(?:\n[^\n]+){1,5}?)(?=\n[A-Z][a-zA-Z\s]+:|\nRegistration|\nPeriod)",
        raw,
        re.IGNORECASE,
    )
    if addr_m:
        address = " ".join(addr_m.group(1).split()).strip()

    rto_name = ""
    rto_m = re.search(r"RTO\s+(?:Location|City)\s*[:\n]+\s*([A-Za-z\s]+)-([A-Za-z\s]+)", raw, re.IGNORECASE)
    if rto_m:
        rto_state = rto_m.group(1).strip()
        rto_city = rto_m.group(2).strip()
        rto_name = f"{rto_state}-{rto_city}"
        if not city:
            city = rto_city
        if not state:
            state = rto_state

    pin_m = re.search(r"\b([1-9]\d{5})\b", address or raw)
    if pin_m:
        pin = pin_m.group(1)

    if not city or not state:
        c_city, c_state = _extract_city_state_from_address(address, full_text=raw)
        if not city:
            city = c_city
        if not state:
            state = c_state

    # ── Contact Info (Masked) ──────────────────────────────────────────────
    mob_m = re.search(r"Mobile\s+No\.?\s*[:\s]*([0-9*]{10,12})", raw, re.IGNORECASE)
    mobile_number = mob_m.group(1).strip() if mob_m else ""
    phone_number = mobile_number

    email_m = re.search(r"Email\s+Address\s*[:\n]+\s*([A-Za-z0-9*._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", raw, re.IGNORECASE)
    email = ""
    if email_m:
        cand_email = email_m.group(1).strip()
        if "customersupport@icicilombard.com" not in cand_email.lower():
            email = cand_email

    # ── Vehicle Details ────────────────────────────────────────────────────
    vehicle_number = ""
    reg_m = re.search(r"(?:Vehicle\s+Registration\s+No\.?|Registration\s+No\.?)\s*[:\n]+\s*([A-Za-z0-9-]+)", raw, re.IGNORECASE)
    if reg_m:
        vehicle_number = reg_m.group(1).strip().replace(" ", "").replace("-", "")

    make = ""
    model_variant = ""
    vm_m = re.search(r"Vehicle\s+Make\s*/\s*Model\s*[:\n]+\s*([^\n/]+)\s*/\s*([^\n]+)", raw, re.IGNORECASE)
    if vm_m:
        make = vm_m.group(1).strip()
        model_variant = vm_m.group(2).strip()

    year_of_manufacture = ""
    yr_m = re.search(r"(?:Mfg\s+Yr|Vehicle\s+Registration\s+Date)\s*[:\n]+\s*([^\n]+)", raw, re.IGNORECASE)
    if yr_m:
        y_match = re.search(r"\b(19\d\d|20\d\d)\b", yr_m.group(1))
        if y_match:
            year_of_manufacture = y_match.group(1)

    chassis_number = ""
    ch_m = re.search(r"Chassis\s+No\.?\s*[:\n]+\s*([A-Za-z0-9]+)", raw, re.IGNORECASE)
    if ch_m:
        cand_ch = ch_m.group(1).strip()
        if cand_ch.lower() not in ["engine", "vehicle", "trailer"]:
            chassis_number = cand_ch

    engine_number = ""
    en_m = re.search(r"Engine\s+No\.?\s*[:\n]+\s*([A-Za-z0-9]+)", raw, re.IGNORECASE)
    if en_m:
        cand_en = en_m.group(1).strip()
        if cand_en.lower() not in ["chassis", "vehicle"]:
            engine_number = cand_en

    cc_kwh = ""
    cc_m = re.search(r"\bCC(?:/KW)?\s*[:\n]+\s*([0-9]+)", raw, re.IGNORECASE)
    if cc_m:
        cc_kwh = cc_m.group(1).strip()

    passenger = ""
    seat_m = re.search(r"(?:Seating\s+Capacity|Carrying\s+Capacity)\s*[:\n]+\s*([0-9]+)", raw, re.IGNORECASE)
    if seat_m:
        passenger = seat_m.group(1).strip()

    # ── NCB ────────────────────────────────────────────────────────────────
    current_ncb = "0"
    ncb_m = re.search(r"(?:No\s+Claim\s+Bonus|Current\s+Year\s+NCB\(%\))\s*[:\n\s]*([0-9]+(?:\.[0-9]+)?)\s*%", raw, re.IGNORECASE)
    if ncb_m:
        current_ncb = ncb_m.group(1).strip()

    previous_ncb = "0"
    pncb_m = re.search(r"Previous\s+Year\s+NCB\(%\)\s*[:\n\s]*([0-9]+(?:\.[0-9]+)?)\s*%", raw, re.IGNORECASE)
    if pncb_m:
        previous_ncb = pncb_m.group(1).strip()

    # ── Dates ──────────────────────────────────────────────────────────────
    policy_issue_date = ""
    iss_m = re.search(r"(?:Policy\s+Issued\s+On|Date)\s*[:\n]+\s*([A-Za-z]{3}\s+\d{1,2},\s+\d{4}|\d{2}[-/]\d{2}[-/]\d{4})", raw, re.IGNORECASE)
    if iss_m:
        policy_issue_date = _normalize_date_only_string(iss_m.group(1).strip())

    policy_start = ""
    policy_end = ""
    period_m = re.search(
        r"Period\s+of\s+Insurance(?:\s*-\s*Own\s+Damage)?\s*[:\n]+\s*([A-Za-z]{3}\s+\d{1,2},\s+\d{4}|\d{2}[-/]\d{2}[-/]\d{4})(?:\s+00:00)?\s+to\s+(?:Midnight\s+of\s+)?([A-Za-z]{3}\s+\d{1,2},\s+\d{4}|\d{2}[-/]\d{2}[-/]\d{4})",
        raw,
        re.IGNORECASE,
    )
    if period_m:
        policy_start = _normalize_date_only_string(period_m.group(1).strip())
        policy_end = _normalize_date_only_string(period_m.group(2).strip())

    tp_start = ""
    tp_end = ""
    if is_saod:
        tp_period_m = re.search(
            r"(?:Third\s+Party\s+Insurance\s+Period|Third\s+Party\s+Policy\s+No\..*?valid\s+from)\s*[:\n\s]+([A-Za-z]{3}\s+\d{1,2},\s+\d{4}|\d{2}[-/]\d{2}[-/]\d{4})\s+to\s+([A-Za-z]{3}\s+\d{1,2},\s+\d{4}|\d{2}[-/]\d{2}[-/]\d{4})",
            raw,
            re.IGNORECASE,
        )
        if tp_period_m:
            tp_start = _normalize_date_only_string(tp_period_m.group(1).strip())
            tp_end = _normalize_date_only_string(tp_period_m.group(2).strip())

    # ── IDV & Premiums ─────────────────────────────────────────────────────
    idv_m = re.search(r"Total\s+IDV\s*\([^)]*\)\s*[:\n`₹Rs\.\s]*([0-9,]+(?:\.\d{2})?)", raw, re.IGNORECASE)
    idv = _cnum(idv_m.group(1)) if idv_m else ""

    od_m = re.search(r"Total\s+Own\s+Damage\s+Premium\s*\(A\)\s*[:\n`₹Rs\.\s]*([0-9,]+(?:\.\d{2})?)", raw, re.IGNORECASE)
    tp_m = re.search(r"Total\s+Liability\s+Premium\s*\(B\)\s*[:\n`₹Rs\.\s]*([0-9,]+(?:\.\d{2})?)", raw, re.IGNORECASE)
    pkg_m = re.search(r"Total\s+Package\s+Premium\s*\(A\+B\)\s*[:\n`₹Rs\.\s]*([0-9,]+(?:\.\d{2})?)", raw, re.IGNORECASE)
    gst_m = re.search(r"Total\s+Tax\s+Payable\s+in\s+[^:\n]*[:\n`₹Rs\.\s]*([0-9,]+(?:\.\d{2})?)", raw, re.IGNORECASE)
    tot_m = re.search(r"Total\s+Premium\s+Payable\s+in\s+[^:\n]*[:\n`₹Rs\.\s]*([0-9,]+(?:\.\d{2})?)", raw, re.IGNORECASE)

    od_net_premium = _cnum(od_m.group(1)) if od_m else ""
    tp_net_premium = "" if is_saod else (_cnum(tp_m.group(1)) if tp_m else "")
    gst = _cnum(gst_m.group(1)) if gst_m else ""
    gross_premium = _cnum(tot_m.group(1)) if tot_m else ""
    payment_amount = gross_premium
    net_premium = od_net_premium if is_saod else (_cnum(pkg_m.group(1)) if pkg_m else od_net_premium)

    # ── CPA & Payment Details ──────────────────────────────────────────────
    cpa = "No"
    pa_m = re.search(r"PA\s+Cover\s+for\s+Owner[- ]Driver\s*[:\n`₹Rs\.\s]*([0-9,]+(?:\.\d{2})?)", raw, re.IGNORECASE)
    if pa_m:
        try:
            val = float(pa_m.group(1).replace(",", ""))
            if val > 0:
                cpa = "Yes"
        except ValueError:
            pass

    payment_mode = "Online"
    if re.search(r"\bCheque\s*(?:No\.?|Number)\b", raw, re.IGNORECASE):
        payment_mode = "Cheque"

    return {
        "insurer":             insurer,
        "business_type":       business_type,
        "policy_type":         pt,
        "policy_number":       policy_number,
        "customer_name":       customer_name,
        "address":             address,
        "city":                city,
        "state":               state,
        "pin":                 pin,
        "phone_number":        phone_number,
        "mobile_number":       mobile_number,
        "dob":                 "",
        "email":               email,
        "vehicle_number":      vehicle_number,
        "make":                make,
        "model_variant":       model_variant,
        "year_of_manufacture": year_of_manufacture,
        "chassis_number":      chassis_number,
        "engine_number":       engine_number,
        "cc_kwh":              cc_kwh,
        "fuel_type":           "",
        "rto_name":            rto_name,
        "current_ncb":         current_ncb,
        "previous_ncb":        previous_ncb,
        "passenger":           passenger,
        "gvw":                 "",
        "policy_issue_date":   policy_issue_date,
        "policy_start":        policy_start,
        "policy_end":          policy_end,
        "tp_start":            tp_start,
        "tp_end":              tp_end,
        "txn_date":            policy_issue_date,
        "idv":                 idv,
        "od_net_premium":      od_net_premium,
        "tp_net_premium":      tp_net_premium,
        "gst":                 gst,
        "gross_premium":       gross_premium,
        "net_premium":         net_premium,
        "cpa":                 cpa,
        "payment_mode":        payment_mode,
        "cheque_number":       "",
        "bank_name":           "",
        "payment_amount":      payment_amount,
        "needs_review":        False,
        "review_reason":       None,
    }


def _fallback_extract_fields(policy_text: str, company_mode: str | None = None) -> dict:
    text = policy_text or ""
    is_icici = company_mode == "ICICI Lombard" or (
        company_mode is None and _is_icici_policy_document(text)
    )
    if is_icici:
        return _extract_icici_fields(text)

    is_tata = company_mode == "TATA AIG" or (
        company_mode is None and _is_tata_policy_document(text)
    )
    if is_tata:
        return _extract_tata_fields(text)

    is_go_digit = company_mode == "GO DIGIT" or (
        company_mode is None and _is_go_digit_policy_document(text)
    )
    if is_go_digit:
        return _extract_go_digit_fields(text)

    # Join multiline split tokens for VINs/Chassis (e.g. MA1ZN2TTKP1G6 \n 0666)
    cleaned_text_for_vin = re.sub(r'([A-Z0-9]{5,15})\s*\n\s*([A-Z0-9]{3,8})\b', r'\1\2', text)
    proposer_source_text = text
    is_zuno = company_mode == "ZUNO" or (
        company_mode is None and _is_zuno_policy_document(text)
    )
    is_hdfc = company_mode == "HDFC ERGO" or (
        company_mode is None and _is_hdfc_policy_document(text)
    )
    if is_zuno:
        zuno_proposer_text = _extract_zuno_proposer_details_section(text)
        if zuno_proposer_text.strip():
            proposer_source_text = zuno_proposer_text

    def _find(pattern: str, source: str = text) -> str:
        match = re.search(pattern, source, flags=re.IGNORECASE | re.MULTILINE)
        if not match:
            return ""
        value = match.group(1) if match.lastindex else match.group(0)
        return str(value).strip()

    def _find_in(source_text: str, pattern: str) -> str:
        return _find(pattern, source_text)

    # Insurer Name
    if is_zuno:
        insurer = "Zuno General Insurance Limited"
    elif is_hdfc:
        insurer = "HDFC ERGO General Insurance Company Limited"
    elif is_go_digit:
        insurer = "Go Digit General Insurance Limited"
    elif is_tata:
        insurer = "TATA AIG General Insurance Company Limited"
    else:
        ins_m = re.search(r"(?:insurance\s*company|insurer)\s*[:#-]?\s*([A-Za-z0-9 .,'-]+)", text, re.IGNORECASE)
        insurer = ins_m.group(1).strip() if ins_m else ""

    # Customer Name
    if is_hdfc:
        cust_m = re.search(
            r"(?:Insured\s+Name|Name\s+of\s+Insured|Name\s+of\s+the\s+Insured)\s*[:#-]?\s*([A-Za-z .'-]+)",
            proposer_source_text,
            re.IGNORECASE,
        )
        if not cust_m:
            cust_m = re.search(
                r"(?:insured|customer|proposer)\s*(?:name)?\s*[:#-]?\s*([A-Za-z .'-]+)",
                proposer_source_text,
                re.IGNORECASE,
            )
    elif is_tata:
        # TATA AIG: 'Name' label in 'Insured Details' block on Page 1
        cust_m = re.search(
            r"(?m)^Name\s*\n([A-Za-z][A-Za-z .'-]+)",
            proposer_source_text,
            re.IGNORECASE,
        )
        if not cust_m:
            cust_m = re.search(
                r"Insured\s+Details[^\n]*\n[\s\S]*?(?:^|\n)Name\s*\n?\s*([A-Za-z][A-Za-z .'-]+)",
                proposer_source_text,
                re.IGNORECASE | re.MULTILINE,
            )
        if not cust_m:
            # Also try '1. Name (Registered owner of the Motor Vehicle): <name>' from transcript
            cust_m = re.search(
                r"1\.\s+Name\s*(?:\(Registered[^)]*\))?\s*[:#-]?\s*([A-Za-z][A-Za-z .'-]+)",
                proposer_source_text,
                re.IGNORECASE,
            )
    else:
        cust_m = re.search(r"Proposer\s+Name\s*\n?\s*([A-Za-z .'-]+)", proposer_source_text, re.IGNORECASE)
        if not cust_m:
            cust_m = re.search(r"(?:insured|customer|proposer)\s*(?:name)?\s*[:#-]?\s*([A-Za-z .'-]+)", proposer_source_text, re.IGNORECASE)
    customer_name = cust_m.group(1).strip() if cust_m else ""
    customer_name = re.sub(r"\s+(?:Vehicle|Policy|Address|Contact|Date|Gender|PAN|GST|Occupation).*", "", customer_name, flags=re.IGNORECASE).strip()

    # Policy number
    if is_hdfc:
        # HDFC policy numbers are long numeric/alphanumeric strings like '1111222233334444000'
        pol_no_m = re.search(
            r'Policy\s*(?:No\.?|Number|#)\s*(?:is\s*)?[:#-]?\s*([A-Za-z0-9/-]{8,})',
            text, re.IGNORECASE,
        )
    elif is_tata:
        # TATA AIG: Policy No. is printed with spaces, e.g. '9876543210 01 00'
        pol_no_m = re.search(
            r'Policy\s*(?:No\.?|Number|#)\s*(?:is\s*)?[:#-]?\s*([0-9][0-9 ]{7,20})',
            text, re.IGNORECASE,
        )
    else:
        pol_no_m = re.search(r'Policy\s*(?:No\.?|Number|#)\s*(?:is\s*)?[:#-]?\s*([A-Za-z0-9/-]{5,})', text, re.IGNORECASE)
    pol_no = ""
    if pol_no_m and pol_no_m.group(1).lower() not in ('is', 'no', 'number', 'details', 'schedule'):
        pol_no = pol_no_m.group(1).strip()

    # Business Type
    bt_m = re.search(r'Business\s*Type\s*[:#-]?\s*([A-Za-z0-9\s-]+)', text, re.IGNORECASE)
    business_type = ""
    if bt_m:
        raw_bt = bt_m.group(1).strip()
        if "\n" in raw_bt:
            raw_bt = raw_bt.split("\n")[0].strip()
        if raw_bt.lower() not in ('type', 'details'):
            business_type = raw_bt


    # Chassis & Engine
    chassis_m = re.search(r'Chassis\s*(?:No\.?|Number)?\s*[:#-]?\s*\n?\s*([A-Za-z0-9]{5,20})', text, re.IGNORECASE)
    if not chassis_m and 'cleaned_text_for_vin' in locals():
        chassis_m = re.search(r'Chassis\s*(?:No\.?|Number)?\s*[:#-]?\s*([A-Z0-9]{8,20})', cleaned_text_for_vin)
    chassis_number = ""
    if chassis_m and chassis_m.group(1).lower() not in ('insured', 'make', 'model', 'number', 'details', 'trailer'):
        chassis_number = chassis_m.group(1).strip()

    engine_m = re.search(r'Engine\s*(?:No\.?|Number)?\s*[:#-]?\s*\n?\s*([A-Za-z0-9]{5,20})', text, re.IGNORECASE)
    engine_number = ""
    if engine_m and engine_m.group(1).lower() not in ('number', 'details', 'make', 'chassis'):
        engine_number = engine_m.group(1).strip()

    # Make & Model Variant
    make_m = re.search(r'Vehicle\s+Make\s*\n?\s*([^\n\r]+)', text, re.IGNORECASE)
    mod_var_m = re.search(r'Vehicle\s+Model\s+and\s+Variant\s*\n?\s*([^\n\r]+)', text, re.IGNORECASE)
    if make_m and mod_var_m:
        make = make_m.group(1).strip()
        model_variant = mod_var_m.group(1).strip().replace('&', ' ')
        model_variant = re.sub(r'\s+', ' ', model_variant)
    else:
        make_model = _extract_make_model(text)
        variant = _extract_full_variant(text)
        make = ""
        model_variant = ""
        if make_model:
            parts = make_model.split()
            if parts:
                make = parts[0].strip()
                model_variant = " ".join(parts[1:]).strip()
        if variant:
            if model_variant:
                if variant.lower() not in model_variant.lower():
                    model_variant = f"{model_variant} {variant}".strip()
            else:
                model_variant = variant

    # RTO Name / Registration Place
    rto_m = re.search(r'Registration\s+Place\s*\n?\s*([^\n\r]+)', text, re.IGNORECASE)
    rto_name = rto_m.group(1).strip() if rto_m else (_find(r"rto\s*(?:name)?\s*[:#-]?\s*([A-Za-z0-9 .,'-]+)") or "")

    zuno_policy_type = _extract_zuno_policy_type_from_page_2(text)
    parsed_address = _find_in(
        proposer_source_text,
        r"address\s*(?:with\s+pincode\s*)?[:#-]?\s*([^\n]+(?:\n(?!\s*(?:pin|postal|phone|mobile|email|policy|vehicle|registration|engine|chassis|rto|fuel|variant|insured|customer|proposer)\b)[^\n]+)*)",
    ) or ""
    parsed_address = re.sub(r"^(?:with\s+Pincode\s*|[:#-]\s*)", "", parsed_address, flags=re.IGNORECASE).strip()
    parsed_city, parsed_state = _extract_city_state_from_address(parsed_address, proposer_source_text)

    cc_kwh = _find(r"\b([0-9]{2,4})\s*cc\b") or _find(r"\b([0-9]{2,4})\s*kwh\b") or ""
    policy_start = (
        _find(r"period\s*of\s*insurance\s*from\s*[:#-]?\s*([0-9]{2}/[0-9]{2}/[0-9]{4})")
        or _find(r"policy\s*start\s*[:#-]?\s*([0-9/]+)")
        or _find(r"start\s*date\s*[:#-]?\s*([0-9/]+)")
        or ""
    )
    policy_end = (
        _find(r"\bto\s*[:#-]?\s*(?:midnight\s*on\s*)?([0-9]{2}/[0-9]{2}/[0-9]{4})")
        or _find(r"policy\s*end\s*[:#-]?\s*([0-9/]+)")
        or _find(r"end\s*date\s*[:#-]?\s*([0-9/]+)")
        or ""
    )

    vehicle_number = _find(
        r"(?:vehicle|registration)\s*(?:mark\s*&\s*)?(?:no\.?|number)\s*[:#-]?\s*([A-Za-z0-9 -]{5,20})"
    ) or ""
    if is_go_digit and not vehicle_number:
        vehicle_number = _find(
            r"(?m)^Vehicle\s*\n\s*([A-Za-z]{2}[A-Za-z0-9 -]{4,18})\s*$"
        ) or ""

    net_prem_m = re.search(r"(?:final|net)\s+premium\s*(?:\(Rs\.\))?\s*[₹:\s]*([0-9,]+(?:\.[0-9]{1,2})?)", text, re.IGNORECASE)
    if net_prem_m:
        net_premium = net_prem_m.group(1).strip()
    else:
        net_premium = (
            _find(r"net\s*premium\s*[:#-]?\s*([0-9,]+(?:\.[0-9]{1,2})?)") or
            # HDFC ERGO: Total Premium (after GST) = effective net premium
            _find(r"Total\s+Premium\s*\n\s*([0-9,]+(?:\.[0-9]{1,2})?)") or
            _find(r"Total\s+Premium\s*[(`)₹\s]+([0-9,]+(?:\.[0-9]{1,2})?)") or
            ""
        )

    # HDFC ERGO / TATA AIG: policy numbers sometimes have spaces
    # (e.g. HDFC: '1111 2222 3333 4444 000', TATA: '9876543210 01 00')
    if (is_hdfc or is_tata) and pol_no:
        pol_no = re.sub(r'\s+', '', pol_no)


    return {
        "policy_number":       pol_no,
        "customer_name":       customer_name,
        "address":             parsed_address,
        "city":                parsed_city,
        "state":               parsed_state,
        "pin":                 _find_in(proposer_source_text, r"\b(?:pin|postal)\s*(?:code)?\s*[:#-]?\s*(\d{6})\b") or "",
        "phone_number":        (
            _find_in(proposer_source_text, r"(?:phone|contact)\s*(?:no|number)?\s*[:#-]?\s*([0-9X*+][0-9X*+\s-]{6,14})") or
            # HDFC prints numbers as "Tel. 98XXXXXXX0": preserve masking chars
            _find_in(proposer_source_text, r"(?:Tel|Ph)[.:]?\s*([0-9][0-9Xx*]{6,14})") or
            # Also search in full text (HDFC address block may not be in proposer_source_text)
            _find(r"(?:Tel|Ph)[.:]?\s*([0-9][0-9Xx*]{6,14})") or
            ""
        ),
        "mobile_number":       (
            _find_in(proposer_source_text, r"mobile\s*(?:no|number)?\s*[:#-]?\s*([0-9X*+][0-9X*+\s-]{9,14})") or
            (
                (_find_in(proposer_source_text, r"(?:Tel|Ph)[.:]?\s*([0-9][0-9Xx*]{9,14})") or
                 _find(r"(?:Tel|Ph)[.:]?\s*([0-9][0-9Xx*]{9,14})"))
                if is_hdfc else ""
            ) or
            ""
        ),
        "dob":                 _extract_proposer_dob(text, company_mode=company_mode),
        "email":               _extract_customer_email(text, company_mode=company_mode),
        "insurer":             insurer,
        "business_type":       (
            business_type or
            # HDFC ERGO: determine New Business / Renewal / Rollover from
            # previous policy details using the dedicated helper.
            (_determine_hdfc_business_type(text) if is_hdfc else "") or
            (_determine_go_digit_business_type(text) if is_go_digit else "")
        ),
        "policy_type":         zuno_policy_type or _find(r"policy\s*(?:type|product)\s*[:#-]?\s*([A-Za-z0-9 .,'-]+)") or "",
        "vehicle_number":      vehicle_number,
        "make":                make,
        "model_variant":       model_variant,
        "year_of_manufacture": _find(r"year\s*of\s*manuf\w*\s*[:#-]?\s*([0-9]{4})") or "",
        "chassis_number":      chassis_number,
        "engine_number":       engine_number,
        "cc_kwh":              cc_kwh,
        "fuel_type":           (
            # HDFC: "Fuel" label appears alone on a line; value on next line
            _find(r"(?m)^Fuel\s*\n([A-Za-z][A-Za-z/]*)") or
            _find(r"(?m)^Fuel\s+Type\s*\n([A-Za-z][A-Za-z/]*)") or
            _find(r"fuel\s*type\s*[:#-]?\s*([A-Za-z0-9 .,'-]+)") or
            # Contextual: if ELECTRIC vehicle detected
            ("Electric" if re.search(r"\bElectric(?:al)?\s+(?:Vehicle|Rickshaw|Rikshaw|Scooter|Car|Motor)", text, re.IGNORECASE) else "") or
            ""
        ),
        "rto_name":            rto_name,
        # NCB: extract the percentage (or 0), never a rupee amount.
        "current_ncb":         (
            _find(r"No\s+Claim\s+Discount\s*\(\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*\)") or
            _find(r"(?:current\s*)?ncb\s*%?\s*[:#-]?\s*([0-9]{1,2})\s*%") or
            "0"
        ),
        "previous_ncb":        (
            _find(r"previous\s*ncb\s*%?\s*[:#-]?\s*([0-9]{1,2})\s*%") or
            "0"
        ),
        "passenger":           _find(r"passenger\s*[:#-]?\s*([0-9]+(?:\s*\+\s*[0-9]+)?)") or "",
        "gvw":                 _find(r"\bgvw\b\s*[:#-]?\s*([A-Za-z0-9, .kg-]+)") or "",
        "policy_issue_date":   (
            _find(r"(?:invoice\s*/\s*issuance|invoice|issuance|issue|issued)\s*date\s*[:#-]?\s*([0-9]{2}/[0-9]{2}/[0-9]{4})") or
            # HDFC: "Issuance Date\n08 Jul 2026"
            _find(r"Issuance\s+Date\s*\n\s*([0-9]{1,2}\s+[A-Za-z]{3}\s+[0-9]{4})") or
            ""
        ),
        "policy_start":        policy_start,
        "policy_end":          policy_end,
        "txn_date":            _find(r"(?:txn|transaction)\s*date\s*[:#-]?\s*([0-9]{2}[./-][0-9]{2}[./-][0-9]{4})") or "",
        "idv":                 _find(r"\bidv\b\s*[:#-]?\s*([A-Za-z0-9, .₹$-]+)") or "",
        "od_net_premium":      (
            # HDFC ERGO: exact label is 'Net Own Damage Premium (a)', value on next line
            _find(r"Net\s+Own\s+Damage\s+Premium\s*\(a\)\s*\n\s*([0-9,]+(?:\.[0-9]{1,2})?)") or
            _find(r"Total\s+own\s+damage\s+premium\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)") or
            _find(r"(?:Net|Total)\s+OD\s+Premium\s*[:#₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)") or
            _find(r"(?:od|own\s*damage)\s*(?:net\s*)?premium\s*[:#-]?\s*([A-Za-z0-9, .₹$-]+)") or
            ""
        ),
        "tp_net_premium":      (
            # HDFC ERGO: exact label is 'Net Liability Premium (b)', value on next line
            _find(r"Net\s+Liability\s+Premium\s*\(b\)\s*\n\s*([0-9,]+(?:\.[0-9]{1,2})?)") or
            # HDFC: Alternative pattern without (b) suffix
            _find(r"Net\s+Liability\s+Premium\s*[:#₹\s]*\n?\s*([0-9,]+(?:\.[0-9]{1,2})?)") or
            _find(r"Total\s+liability\s+premium\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)") or
            _find(r"(?:Net|Total)\s+TP\s+Premium\s*[:#₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)") or
            _find(r"Third\s+Party\s+Premium\s*[:#₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)") or
            _find(r"(?:tp|third\s*party)\s*(?:net\s*)?premium\s*[:#-]?\s*([A-Za-z0-9, .₹$-]+)") or
            ""
        ),
        "gst":                 (lambda t: (
            _find(r"\btotal\s+gst\b\s*[:#-]?\s*([0-9,]+(?:\.[0-9]{1,2})?)") or
            (lambda cm, sm: f"{float(cm.group(1).replace(',','')) + float(sm.group(1).replace(',','')):.2f}"
             if cm and sm else "")(
                re.search(r"CGST\s*@\s*[0-9]+%?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)", t, re.IGNORECASE),
                re.search(r"SGST\s*@\s*[0-9]+%?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)", t, re.IGNORECASE),
            ) or ""
        ))(text),
        "gross_premium":       _find(r"gross\s*premium\s*[:#-]?\s*([A-Za-z0-9, .₹$-]+)") or "",
        "net_premium":         net_premium,
        "needs_review":        bool(not text.strip()),
        "review_reason":       None,
    }


def _clean_text_field(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    cleaned = str(value).strip()
    return cleaned.strip("\"'")


def _is_blankish(value: object) -> bool:
    if value is None:
        return True
    text = str(value).strip().lower()
    return text in {"", "null", "none", "n/a", "na", "not available", "not found"}


_TP_ONLY_KEYWORDS = re.compile(
    r"liability\s+only|third[\s-]*party\s+only|tp\s+only|"
    r"liability\s+policy|tp\s+policy|"
    r"standalone\s+(?:tp|third[\s-]*party)|"
    r"goods\s+carrying\s+liability|"
    r"passenger\s+carrying\s+liability|"
    r"private\s+car\s+liability",
    flags=re.IGNORECASE,
)


def _is_tp_only_policy(enriched: dict, raw_text: str) -> bool:
    """Return True if this is a Third Party / Liability Only policy (no IDV)."""
    policy_type = str(enriched.get("policy_type") or "").strip()
    if _TP_ONLY_KEYWORDS.search(policy_type):
        return True
    # Also check raw text header in case policy_type wasn't extracted yet
    first_500 = raw_text[:500]
    return bool(_TP_ONLY_KEYWORDS.search(first_500))


def _find_first_match(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).strip()
    return ""


_POLICY_NO_RE = re.compile(
    r"policy\s*(?:number|no\.?|#)\s*[:#-]?\s*([A-Za-z0-9/-]+)",
    flags=re.IGNORECASE,
)


def _extract_policy_number_candidates(policy_text: str) -> list[str]:
    # Match all common label variants used by insurers:
    #   "Policy #", "Policy No.", "Policy No:", "Policy Number:", "Policy No :"
    candidates = _POLICY_NO_RE.findall(policy_text or "")
    cleaned: list[str] = []
    for candidate in candidates:
        value = str(candidate).strip()
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned


def _backfill_missing_fields_from_text(
    result: dict, policy_text: str, company_mode: str | None = None
) -> dict:
    if not isinstance(result, dict):
        return result

    text = str(policy_text or "")
    proposer_text, nominee_text = _split_proposer_and_nominee_text(text)
    extraction_text = proposer_text if proposer_text.strip() else text
    enriched = dict(result)

    is_zuno = company_mode == "ZUNO" or (
        company_mode is None and _is_zuno_policy_document(text)
    )
    is_hdfc_doc = company_mode == "HDFC ERGO" or (
        company_mode is None and _is_hdfc_policy_document(text)
    )
    is_tata_doc = company_mode == "TATA AIG" or (
        company_mode is None and _is_tata_policy_document(text)
    )
    is_go_digit_doc = company_mode == "GO DIGIT" or (
        company_mode is None and _is_go_digit_policy_document(text)
    )
    is_icici_doc = company_mode == "ICICI Lombard" or (
        company_mode is None and _is_icici_policy_document(text)
    )

    if is_go_digit_doc or is_tata_doc or is_hdfc_doc or is_icici_doc:
        fallback = _fallback_extract_fields(text, company_mode=company_mode)
    else:
        fallback = _fallback_extract_fields(extraction_text, company_mode=company_mode)

    if is_go_digit_doc:
        for field, value in fallback.items():
            if field not in {'needs_review', 'review_reason'}:
                cur_val = enriched.get(field)
                if _is_blankish(cur_val) or (str(cur_val).strip() in ('0', '0.0', '0.00', '') and str(value).strip() not in ('', '0', '0.0', '0.00')):
                    if not _is_blankish(value):
                        enriched[field] = value

        # Overwrite invalid, truncated or 0001 year_of_manufacture
        cur_yr = str(enriched.get('year_of_manufacture') or '').strip()
        if not re.fullmatch(r'(?:19|20)\d{2}', cur_yr):
            y_m = re.search(r'\b((?:19|20)\d{2})\b', cur_yr)
            if y_m:
                enriched['year_of_manufacture'] = y_m.group(1)
            elif fallback.get('year_of_manufacture'):
                enriched['year_of_manufacture'] = fallback['year_of_manufacture']

        # Backfill GVW for Commercial vehicles
        cur_gvw = str(enriched.get('gvw') or '').strip()
        if cur_gvw in ('0', '0.0', '0.00', '', 'None') and fallback.get('gvw'):
            enriched['gvw'] = fallback['gvw']

        # Overwrite swapped or identical policy dates
        st_val = str(enriched.get('policy_start') or '').strip()
        en_val = str(enriched.get('policy_end') or '').strip()
        if st_val == en_val or _is_blankish(st_val) or _is_blankish(en_val):
            if fallback.get('policy_start') and fallback.get('policy_end'):
                enriched['policy_start'] = fallback['policy_start']
                enriched['policy_end'] = fallback['policy_end']

        if fallback.get('policy_number') and (
            _is_blankish(enriched.get('policy_number'))
            or not str(enriched.get('policy_number', '')).startswith('D')
            or ' ' in str(enriched.get('policy_number', ''))
        ):
            enriched['policy_number'] = fallback['policy_number']

        if fallback.get('business_type'):
            enriched['business_type'] = fallback['business_type']

        if fallback.get('policy_type'):
            enriched['policy_type'] = fallback['policy_type']

        if fallback.get('vehicle_number') and _is_blankish(enriched.get('vehicle_number')):
            enriched['vehicle_number'] = fallback['vehicle_number']

        # Clean city & state if noisy
        if fallback.get('city') and (
            _is_blankish(enriched.get('city'))
            or any(w in str(enriched.get('city', '')).upper() for w in ('TANKI', 'NEAR', 'BHAJANPURA', 'PRIYADARS'))
        ):
            enriched['city'] = fallback['city']
        if fallback.get('state') and (
            _is_blankish(enriched.get('state'))
            or str(enriched.get('state', '')).endswith('-')
            or re.search(r'\d', str(enriched.get('state', '')))
        ):
            enriched['state'] = fallback['state']
        if fallback.get('pin') and _is_blankish(enriched.get('pin')):
            enriched['pin'] = fallback['pin']

        # Payment Mode: Always Online for electronic issuance unless explicit cheque
        if re.search(r'Cheque\s+No\.?\s*[:#-]?\s*(\d{6})\b', text, re.IGNORECASE):
            enriched['payment_mode'] = 'Cheque'
        else:
            enriched['payment_mode'] = 'Online'

        # Sanitize txn_date: Ensure it is a clean date string and never mixed with policy numbers
        raw_txn = str(enriched.get('txn_date') or '').strip()
        clean_txn = ''
        if raw_txn:
            d_m = re.search(r'(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}-[A-Za-z]{3}-\d{4})', raw_txn)
            if d_m:
                clean_txn = _normalize_date_only_string(d_m.group(1))
            elif re.search(r'\b\d{8}\b', raw_txn):
                raw_digits = re.search(r'\b(\d{2})(\d{2})(\d{4})\b', raw_txn)
                if raw_digits:
                    clean_txn = f'{raw_digits.group(1)}/{raw_digits.group(2)}/{raw_digits.group(3)}'
        if not clean_txn:
            clean_txn = fallback.get('txn_date') or fallback.get('policy_issue_date') or ''
        enriched['txn_date'] = clean_txn

        return enriched

    if is_zuno:
        zuno_policy_type = _extract_zuno_policy_type_from_page_2(text)
        if zuno_policy_type:
            current_policy_type = _normalize_policy_type(str(enriched.get("policy_type") or ""), is_zuno=True)
            expected_policy_type = _normalize_policy_type(zuno_policy_type, is_zuno=True)
            if not current_policy_type or current_policy_type.lower() != expected_policy_type.lower():
                enriched["policy_type"] = expected_policy_type

    total_od = _find_first_match(
        extraction_text,
        [r"Total\s+own\s+damage\s+premium\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)"],
    )
    if total_od:
        enriched["od_net_premium"] = total_od

    total_tp = _find_first_match(
        extraction_text,
        [r"Total\s+liability\s+premium\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)"],
    )
    if total_tp:
        enriched["tp_net_premium"] = total_tp

    if _is_blankish(enriched.get("policy_number")):
        policy_numbers = _extract_policy_number_candidates(extraction_text)
        preferred = next((n for n in policy_numbers if re.match(r"^[A-Za-z]\d{6,}$", n)), "")
        enriched["policy_number"] = preferred or (policy_numbers[-1] if policy_numbers else "")

    if _is_blankish(enriched.get("customer_name")):
        enriched["customer_name"] = _find_first_match(
            extraction_text,
            [
                r"\n\s*([A-Z][A-Z .'-]{2,})\s+Policy\s*#",
                r"\n\s*Insured\s*Name\s*[:#-]?\s*([A-Za-z .'-]+)",
            ],
        )

    if _is_blankish(enriched.get("insurer")):
        enriched["insurer"] = _find_first_match(
            extraction_text,
            [
                r"\n\s*(IFFCO[-\s]*TOKIO\s+GENERAL\s+INSURANCE\s+CO\.?\s*LTD)\b",
                r"\n\s*([A-Za-z][A-Za-z&.,'\-\s]{8,}INSURANCE[A-Za-z&.,'\-\s]{0,40})\n",
            ],
        )

    if _is_blankish(enriched.get("policy_type")):
        # HDFC ERGO: extract full plan name from the subtitle header line
        if is_hdfc_doc:
            hdfc_pt_m = re.search(
                r"(?:Certificate\s+of\s+Insurance\s+cum\s+Policy\s+Schedule|Motor\s+Insurance)\s*\n\s*([^\n]{10,120})",
                text,
                flags=re.IGNORECASE,
            )
            if hdfc_pt_m:
                enriched["policy_type"] = hdfc_pt_m.group(1).strip()
        if _is_blankish(enriched.get("policy_type")):
            if re.search(r"commercial\s+vehicle", extraction_text, flags=re.IGNORECASE):
                enriched["policy_type"] = "Commercial Vehicle"
            elif re.search(r"two[\s-]*wheeler", extraction_text, flags=re.IGNORECASE):
                enriched["policy_type"] = "Two Wheeler"
            elif re.search(r"private\s+(?:car|motor\s+car)", extraction_text, flags=re.IGNORECASE):
                enriched["policy_type"] = "Private Car"
            elif re.search(r"third\s*party", extraction_text, flags=re.IGNORECASE):
                enriched["policy_type"] = "Third Party"
            else:
                # Try to grab whatever the document calls its product/policy type
                pt = _find_first_match(extraction_text, [
                    r"policy\s+type\s*[:#-]?\s*([A-Za-z0-9 &.,'-]+)",
                    r"product\s+(?:name|type)\s*[:#-]?\s*([A-Za-z0-9 &.,'-]+)",
                ])
                if pt:
                    enriched["policy_type"] = _normalize_policy_type(pt)

    if _is_blankish(enriched.get("vehicle_number")):
        enriched["vehicle_number"] = _find_first_match(
            extraction_text,
            [
                r"\b([A-Z]{2}\d{1,2}[A-Z]{1,3}\d{1,4})\b",
                r"registration\s*mark\s*&\s*no\.?\s*[:#-]?\s*([A-Za-z0-9-]+)",
            ],
        )

    if _is_blankish(enriched.get("year_of_manufacture")):
        enriched["year_of_manufacture"] = _find_first_match(
            extraction_text,
            [
                r"year\s+of\s+manuf\w*\.?\s*\n?\s*([12][0-9]{3})",
                r"\b([12][0-9]{3})\b\s*\n\s*make\s+of\s+vehicle",
            ],
        )

    if _is_blankish(enriched.get("cc_kwh")):
        enriched["cc_kwh"] = _find_first_match(
            extraction_text,
            [
                r"\b([0-9]{2,4})\s+Package\b",
                r"\b([0-9]{2,4})\s*(?:cc|kwh)\b",
            ],
        )

    if _is_blankish(enriched.get("passenger")):
        enriched["passenger"] = _find_first_match(
            extraction_text,
            [
                r"\b([0-9]+\s*\+\s*[0-9]+)\b",
                r"licensed\s+carrying\s+capacity\s*\n?\s*([0-9]+)",
            ],
        )

    if _is_blankish(enriched.get("policy_issue_date")):
        enriched["policy_issue_date"] = _find_first_match(
            extraction_text,
            [
                r"invoice\s*/\s*issuance\s*date\s*[:#-]?\s*([0-9]{2}/[0-9]{2}/[0-9]{4})",
                r"\bdate\s*:\s*([0-9]{2}\.[0-9]{2}\.[0-9]{4})",
            ],
        )

    if _is_blankish(enriched.get("policy_start")):
        enriched["policy_start"] = _find_first_match(
            extraction_text,
            [
                r"period\s*of\s*insurance\s*from\s*[:#-]?\s*([0-9]{2}/[0-9]{2}/[0-9]{4})",
                r"from\s*[:#-]?\s*([0-9]{2}/[0-9]{2}/[0-9]{4})",
            ],
        )

    if _is_blankish(enriched.get("policy_end")):
        enriched["policy_end"] = _find_first_match(
            extraction_text,
            [
                r"to\s*[:#-]?\s*(?:midnight\s*on\s*)?([0-9]{2}/[0-9]{2}/[0-9]{4})",
            ],
        )

    if _is_blankish(enriched.get("idv")):
        enriched["idv"] = _find_first_match(
            text,
            [
                r"\bIDV\s+in\s+Rs\.?\s*\n?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
                r"\bIDV\b[^0-9]{0,10}([0-9,]+(?:\.[0-9]{1,2})?)",
            ],
        )

    # TP/Liability-Only policies do not have an IDV: clear any spurious value
    if _is_tp_only_policy(enriched, text):
        enriched["idv"] = "0"

    if _is_blankish(enriched.get("od_net_premium")):
        enriched["od_net_premium"] = _find_first_match(
            text,
            [
                r"Total\s+own\s+damage\s+premium\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)",
                r"(?:od|own\s*damage)\s*(?:net\s*)?premium\s*[:#-]?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
            ],
        )

    if _is_blankish(enriched.get("tp_net_premium")):
        enriched["tp_net_premium"] = _find_first_match(
            text,
            [
                r"Net\s+Liability\s+Premium\s*(?:\([a-z]\))?\s*[:#₹\s]*\n?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
                r"Total\s+liability\s+premium\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)",
                r"(?:tp|third[\s-]*party)\s+net\s+premium\s*[₹:\s]*([0-9,]+(?:\.[0-9]{1,2})?)",
            ],
        )

    if _is_blankish(enriched.get("gst")):
        # Try an explicit "Total GST" label first
        total_gst = _find_first_match(extraction_text, [r"Total\s+GST\s*\n?\s*([0-9,]+(?:\.[0-9]{1,2})?)"])
        if total_gst:
            enriched["gst"] = total_gst
        else:
            # Sum CGST and SGST when listed separately (e.g. Zuno-style policies)
            cgst_match = re.search(r"CGST\s*@\s*[0-9]+%?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)", extraction_text, flags=re.IGNORECASE)
            sgst_match = re.search(r"SGST\s*@\s*[0-9]+%?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)", extraction_text, flags=re.IGNORECASE)
            if cgst_match and sgst_match:
                try:
                    cgst_val = float(cgst_match.group(1).replace(",", ""))
                    sgst_val = float(sgst_match.group(1).replace(",", ""))
                    enriched["gst"] = f"{cgst_val + sgst_val:.2f}"
                except ValueError:
                    pass

    if _is_blankish(enriched.get("gross_premium")):
        enriched["gross_premium"] = _find_first_match(
            extraction_text,
            [
                r"Gross\s+Premium\s+Payable\s*\(Rs\.\)\s*\n?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
                r"\bTotal\s+[0-9,]+(?:\.[0-9]{1,2})?\s+[0-9,]+(?:\.[0-9]{1,2})?\s+[0-9,]+(?:\.[0-9]{1,2})?\s+([0-9,]+(?:\.[0-9]{1,2})?)",
            ],
        )

    if _is_blankish(enriched.get("net_premium")):
        enriched["net_premium"] = _find_first_match(
            extraction_text,
            [
                r"Net\s+Premium\s*\(Rs\.\)\s*\n?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
                r"\bAmount\s+Received\s+([0-9,]+(?:\.[0-9]{1,2})?)",
            ],
        )

    if is_hdfc_doc:
        # For HDFC, always determine business_type using the deterministic
        # rule-based helper: this overrides whatever the AI returned because
        # the three-way logic (New Business / Renewal / Rollover) depends on
        # reading specific fields from the document that the AI can misinterpret.
        enriched["business_type"] = _determine_hdfc_business_type(text)

        # For HDFC, always re-extract NCB from the premium table row
        # "Less: No Claim Bonus (20%)", never use the Previous Policy NCB column.
        hdfc_ncb_m = re.search(
            r"Less\s*:\s*No\s+Claim\s+Bonus\s*\(\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*\)",
            text,
            flags=re.IGNORECASE,
        )
        if not hdfc_ncb_m:
            hdfc_ncb_m = re.search(
                r"Less\s*:\s*No\s+Claim\s+Bonus\s+([0-9]+(?:\.[0-9]+)?)\s*%",
                text,
                flags=re.IGNORECASE,
            )
        if hdfc_ncb_m:
            enriched["current_ncb"] = hdfc_ncb_m.group(1).strip()

        # HDFC: Tel. number in address block is the only contact number.
        # Always extract directly from PDF text (regex) to preserve masking chars
        # like 'X': never trust the AI value which may have replaced Xs with digits.
        tel_m = _HDFC_TEL_RE.search(text)
        tel_number = tel_m.group(1).strip() if tel_m else ""
        if tel_number:
            # Always override: regex on raw PDF text is the ground truth here
            enriched["phone_number"] = tel_number
            enriched["mobile_number"] = tel_number

        # HDFC: Extract CPA (check if any PA rows exist with non-zero amounts)
        if _is_blankish(enriched.get("cpa")):
            pa_rows = re.findall(
                r"(?:PA\s+(?:Cover\s+)?(?:for\s+)?(?:Owner\s+Driver|Un-?Named\s+Persons?|Unnamed\s+Passenger|to\s+Paid\s+Driver|Cover\s+for\s+Owner\s+Driver\s+\(CSI\)))[^\n]*?[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)",
                text,
                flags=re.IGNORECASE,
            )
            if pa_rows:
                try:
                    total_cpa = sum(float(pa.replace(",", "")) for pa in pa_rows)
                    enriched["cpa"] = "Yes" if total_cpa > 0 else "No"
                except ValueError:
                    enriched["cpa"] = "No"
            else:
                enriched["cpa"] = "No"

        # HDFC: Extract payment details from "Payment Details" line
        # Format: "Payment Details : <ref_code> , Bank Name:<bank>"
        # or: "Payment Details : Cheque No. XXXXXX , Bank Name:YYYY"
        if _is_blankish(enriched.get("payment_mode")) or _is_blankish(enriched.get("cheque_number")) or _is_blankish(enriched.get("bank_name")):
            payment_details_m = re.search(
                r"Payment\s+Details\s*[:#-]?\s*([^,\n]+)(?:\s*,\s*Bank\s+Name\s*:\s*([^\n]+))?",
                text,
                flags=re.IGNORECASE,
            )
            if payment_details_m:
                payment_ref = payment_details_m.group(1).strip()
                bank = payment_details_m.group(2).strip() if payment_details_m.group(2) else ""
                
                # Determine payment mode and cheque number
                if "cheque" in payment_ref.lower():
                    # Extract cheque number
                    cheque_m = re.search(r"cheque\s+no\.?\s*([A-Z0-9]+)", payment_ref, flags=re.IGNORECASE)
                    if cheque_m and _is_blankish(enriched.get("payment_mode")):
                        enriched["payment_mode"] = "Cheque"
                    if cheque_m and _is_blankish(enriched.get("cheque_number")):
                        enriched["cheque_number"] = cheque_m.group(1).strip()
                    # Set bank name only for cheque payments
                    if bank and _is_blankish(enriched.get("bank_name")):
                        enriched["bank_name"] = bank
                else:
                    # Alphanumeric reference code (Online/NEFT/UPI): do NOT fill cheque_number or bank_name
                    if _is_blankish(enriched.get("payment_mode")):
                        enriched["payment_mode"] = "Online"

        # HDFC: payment_amount is the Total Premium
        if _is_blankish(enriched.get("payment_amount")):
            total_premium_m = re.search(
                r"Total\s+Premium\s*[₹\s]*\n?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
                text,
                flags=re.IGNORECASE,
            )
            if total_premium_m:
                enriched["payment_amount"] = total_premium_m.group(1).strip()
    elif is_tata_doc:
        # TATA AIG: Business type from Transcript of Proposal
        enriched["business_type"] = _determine_tata_business_type(text)

        # TATA AIG: Extract NCB from 'Less: No claim bonus (X%)' in Schedule of Premium
        tata_ncb_m = re.search(
            r"Less\s*:\s*No\s+claim\s+bonus\s*\(\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*\)",
            text,
            flags=re.IGNORECASE,
        )
        if not tata_ncb_m:
            tata_ncb_m = re.search(
                r"Less\s*:\s*No\s+claim\s+bonus\s+([0-9]+(?:\.[0-9]+)?)\s*%",
                text,
                flags=re.IGNORECASE,
            )
        if tata_ncb_m:
            enriched["current_ncb"] = tata_ncb_m.group(1).strip()

        # TATA AIG: previous NCB from Transcript Of Proposal
        if _is_blankish(enriched.get("previous_ncb")):
            prev_ncb_m = re.search(
                r"NCB\s+in\s+Previous\s+Policy\s*[:#-]?\s*([0-9]+(?:\.[0-9]+)?)\s*%",
                text,
                flags=re.IGNORECASE,
            )
            if prev_ncb_m:
                enriched["previous_ncb"] = prev_ncb_m.group(1).strip()

        # TATA AIG: CPA check
        p1_cpa_m = re.search(r"Compulsory\s+PA\s+Cover\s+for\s+Owner-Driver\s*\n\s*([^\n]+)", text, re.IGNORECASE)
        if p1_cpa_m:
            p1_val = p1_cpa_m.group(1).strip()
            if re.search(r"\d{2}/\d{2}/\d{4}", p1_val) and "NA" not in p1_val.upper():
                enriched["cpa"] = "Yes"
            else:
                enriched["cpa"] = "No"
        elif _is_blankish(enriched.get("cpa")):
            cpa_m = re.search(
                r"Compulsory\s+PA\s+(?:cover|Cover)\s+for\s+Owner[\s-]Driv(?:e|er)[^\n]*?[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)",
                text,
                flags=re.IGNORECASE,
            )
            if cpa_m:
                try:
                    cpa_val = float(cpa_m.group(1).replace(",", ""))
                    enriched["cpa"] = "Yes" if cpa_val > 0 else "No"
                except ValueError:
                    enriched["cpa"] = "No"
            else:
                prop_cpa_m = re.search(r"•CPA\s+Cover\s+Period\s*:\s*([^\n]+)", text, re.IGNORECASE)
                if prop_cpa_m:
                    pval = prop_cpa_m.group(1).strip().upper()
                    if "NA TO NA" in pval or pval == "NA":
                        enriched["cpa"] = "No"
                    elif re.search(r"\d{2}/\d{2}/\d{4}", pval):
                        enriched["cpa"] = "Yes"
                    else:
                        enriched["cpa"] = "No"
                else:
                    enriched["cpa"] = "No"

        # TATA AIG: GST: CGST+SGST or IGST
        if _is_blankish(enriched.get("gst")):
            igst_m = re.search(r"IGST\s*@\s*[0-9]+%?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)", text, re.IGNORECASE)
            if igst_m:
                enriched["gst"] = igst_m.group(1).replace(",", "")
            else:
                cgst_m = re.search(r"CGST\s*@\s*[0-9]+%?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)", text, re.IGNORECASE)
                sgst_m = re.search(r"SGST(?:/UGST)?\s*@\s*[0-9]+%?\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)", text, re.IGNORECASE)
                if cgst_m and sgst_m:
                    try:
                        enriched["gst"] = f"{float(cgst_m.group(1).replace(',','')) + float(sgst_m.group(1).replace(',','')):.2f}"
                    except ValueError:
                        pass

        # TATA AIG: Payment details from Receipt table
        if _is_blankish(enriched.get("payment_mode")) or _is_blankish(enriched.get("payment_amount")):
            rcpt_m = re.search(r"Receipt\s*Receipt\s+No\.[\s\S]*?(?:Notes:|\Z)", text, re.IGNORECASE)
            if not rcpt_m:
                rcpt_m = re.search(r"Mode\s+of\s+Payment[\s\S]*?(?:Notes:|\Z)", text, re.IGNORECASE)
            rcpt_text = rcpt_m.group(0) if rcpt_m else text

            row_m = re.search(
                r"(?:^|\n)\s*1\.\s*\n\s*\d{8,12}\s*(?:\n\s*\d{2}\s+\d{2})?\s*\n\s*([A-Za-z][A-Za-z0-9 _\n-]+?)\s*\n\s*[0-9,]+(?:\.[0-9]{1,2})?\b",
                rcpt_text,
                re.MULTILINE,
            )
            if row_m:
                raw_mode = " ".join(row_m.group(1).split()).strip()
                lowered_mode = raw_mode.lower()
                if any(k in lowered_mode for k in ["online", "paymentlink", "upi", "neft", "imps"]):
                    enriched["payment_mode"] = "Online"
                elif "cheque" in lowered_mode:
                    enriched["payment_mode"] = "Cheque"
                else:
                    enriched["payment_mode"] = raw_mode.title()
            else:
                receipt_mode_m = re.search(
                    r"Mode\s+of\s+Payment\s*([^\n]+)",
                    text,
                    flags=re.IGNORECASE,
                )
                if receipt_mode_m:
                    raw_mode = receipt_mode_m.group(1).strip()
                    lowered_mode = raw_mode.lower()
                    if any(k in lowered_mode for k in ["online", "paymentlink", "upi", "neft", "imps"]):
                        enriched["payment_mode"] = "Online"
                    elif "cheque" in lowered_mode:
                        enriched["payment_mode"] = "Cheque"
                    else:
                        enriched["payment_mode"] = raw_mode.title()

        # TATA AIG: payment_amount from Receipt table
        if _is_blankish(enriched.get("payment_amount")):
            receipt_total_m = re.search(
                r"Total\s+Policy\s+Premium\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)",
                text,
                flags=re.IGNORECASE,
            )
            if not receipt_total_m:
                receipt_total_m = re.search(
                    r"Premium\s+Amount\s*\(Including\s+GST\)\s*[₹\s]*([0-9,]+(?:\.[0-9]{1,2})?)",
                    text,
                    flags=re.IGNORECASE,
                )
            if receipt_total_m:
                enriched["payment_amount"] = receipt_total_m.group(1).replace(",", "")

        # TATA AIG: policy_issue_date from Receipt Date
        if _is_blankish(enriched.get("policy_issue_date")):
            receipt_date_m = re.search(
                r"Receipt\s+Date\s*([0-9]{2}/[0-9]{2}/[0-9]{4})",
                text,
                flags=re.IGNORECASE,
            )
            if receipt_date_m:
                enriched["policy_issue_date"] = receipt_date_m.group(1).strip()

        # TATA AIG: txn_date: same as Receipt Date
        if _is_blankish(enriched.get("txn_date")):
            receipt_date_m2 = re.search(
                r"Receipt\s+Date\s*([0-9]{2}/[0-9]{2}/[0-9]{4})",
                text,
                flags=re.IGNORECASE,
            )
            if receipt_date_m2:
                enriched["txn_date"] = receipt_date_m2.group(1).strip()

        # TATA AIG: OD and TP net premium calculation
        tata_od, tata_tp = _extract_tata_od_tp_premium(text)
        if tata_od:
            enriched["od_net_premium"] = tata_od
        if tata_tp:
            enriched["tp_net_premium"] = tata_tp
        elif "standalone" in str(enriched.get("policy_type", "")).lower() or "saod" in text.lower():
            enriched["tp_net_premium"] = "0.00"

        # TATA AIG: Net Premium & Gross Premium synchronization
        if not _is_blankish(enriched.get("od_net_premium")) and not _is_blankish(enriched.get("tp_net_premium")):
            try:
                _od_f = float(str(enriched["od_net_premium"]).replace(",", ""))
                _tp_f = float(str(enriched["tp_net_premium"]).replace(",", ""))
                _net_f = _od_f + _tp_f
                enriched["net_premium"] = f"{_net_f:.2f}".rstrip("0").rstrip(".")
                if not _is_blankish(enriched.get("gst")):
                    _gst_f = float(str(enriched["gst"]).replace(",", ""))
                    enriched["gross_premium"] = f"{_net_f + _gst_f:.2f}".rstrip("0").rstrip(".")
            except ValueError:
                pass

        # TATA AIG: policy_type from document header
        if _is_blankish(enriched.get("policy_type")):
            tata_pt_m = re.search(
                r"(Auto\s+Secure\s*[-–]\s*[A-Za-z ]+(?:Policy|Cover))",
                text,
                flags=re.IGNORECASE,
            )
            if tata_pt_m:
                raw_pt = tata_pt_m.group(1).strip()
                enriched["policy_type"] = re.sub(r"^[^-–]+[-–]\s*", "", raw_pt).strip()
        else:
            enriched["policy_type"] = re.sub(r"^[^-–]+[-–]\s*", "", str(enriched["policy_type"])).strip()

        # TATA AIG: policy number: format 14-digit TATA policy numbers
        if not _is_blankish(enriched.get("policy_number")):
            enriched["policy_number"] = _format_tata_policy_number(enriched["policy_number"])

        # TATA AIG: insurer always hardcoded
        enriched["insurer"] = "TATA AIG General Insurance Company Limited"

    elif is_icici_doc:
        # ICICI Lombard: Business type from deterministic helper
        enriched["business_type"] = _determine_icici_business_type(text)

        # Insurer always full official name
        enriched["insurer"] = "ICICI Lombard General Insurance Company Limited"

        # Fuel type and GVW are never printed by ICICI
        enriched["fuel_type"] = ""
        enriched["gvw"] = ""

        # Bank and cheque details are left blank (never map Hypothecated To to bank)
        enriched["bank_name"] = ""
        enriched["cheque_number"] = ""

        # Policy type: verify banner / product code
        current_pt = str(enriched.get("policy_type") or "").strip()
        pol_num = str(enriched.get("policy_number") or fallback.get("policy_number") or "")
        is_saod = (
            "stand-alone" in current_pt.lower()
            or "saod" in current_pt.lower()
            or "/o/" in pol_num.lower()
            or re.search(r"Product\s+Code\s*[:\s]+300[15]/O\b", text, re.IGNORECASE) is not None
        )
        if _is_blankish(current_pt) or fallback.get("policy_type"):
            if fallback.get("policy_type"):
                enriched["policy_type"] = fallback["policy_type"]
                is_saod = "stand-alone" in str(enriched["policy_type"]).lower() or "saod" in str(enriched["policy_type"]).lower()

        # Policy number: ensure full 300x format
        if _is_blankish(enriched.get("policy_number")) or not str(enriched.get("policy_number", "")).startswith("300"):
            if fallback.get("policy_number"):
                enriched["policy_number"] = fallback["policy_number"]

        # SAOD vs Package third-party dates & TP net premium
        if is_saod:
            enriched["tp_net_premium"] = "0.00"
            if fallback.get("tp_start") and fallback.get("tp_end"):
                enriched["tp_start"] = fallback["tp_start"]
                enriched["tp_end"] = fallback["tp_end"]
            if not _is_blankish(enriched.get("od_net_premium")):
                enriched["net_premium"] = enriched["od_net_premium"]
        else:
            enriched["tp_start"] = ""
            enriched["tp_end"] = ""
            if fallback.get("tp_net_premium") and _is_blankish(enriched.get("tp_net_premium")):
                enriched["tp_net_premium"] = fallback["tp_net_premium"]

        # Premiums synchronization (net = pre-tax, gross = post-tax final)
        if fallback.get("od_net_premium") and _is_blankish(enriched.get("od_net_premium")):
            enriched["od_net_premium"] = fallback["od_net_premium"]
        if fallback.get("gst") and _is_blankish(enriched.get("gst")):
            enriched["gst"] = fallback["gst"]
        if fallback.get("gross_premium") and _is_blankish(enriched.get("gross_premium")):
            enriched["gross_premium"] = fallback["gross_premium"]
        if fallback.get("net_premium") and _is_blankish(enriched.get("net_premium")):
            enriched["net_premium"] = fallback["net_premium"]

        if not _is_blankish(enriched.get("gross_premium")):
            enriched["payment_amount"] = enriched["gross_premium"]

        # Payment Mode: Online unless cheque number pattern exists
        if re.search(r"\bCheque\s*(?:No\.?|Number)\b", text, re.IGNORECASE):
            enriched["payment_mode"] = "Cheque"
        elif _is_blankish(enriched.get("payment_mode")):
            enriched["payment_mode"] = "Online"

        # Preserve masked contact details
        if fallback.get("mobile_number") and (_is_blankish(enriched.get("mobile_number")) or "*" in fallback["mobile_number"]):
            enriched["mobile_number"] = fallback["mobile_number"]
            enriched["phone_number"] = fallback["mobile_number"]
        if fallback.get("email") and _is_blankish(enriched.get("email")):
            enriched["email"] = fallback["email"]

    elif _is_blankish(enriched.get("current_ncb")):
        enriched["current_ncb"] = _find_first_match(
            extraction_text,
            [
                r"No\s+Claim\s+Discount\s*\(\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*\)",
            ],
        )

    if _is_blankish(enriched.get("financier")):
        enriched["financier"] = _find_first_match(
            extraction_text,
            [
                r"Under\s+Hire\s+Purchase\s*/\s*Hypothecated\s*/\s*Lease\s+Agreement\s+with\s+([^\n]+)",
                r"financ(?:ier|er)\s*[:#-]?\s*([^\n]+)",
            ],
        )

    for field, value in fallback.items():
        if field in {"needs_review", "review_reason"}:
            continue
        if _is_blankish(enriched.get(field)) and not _is_blankish(value):
            enriched[field] = value

    # If model output accidentally copied the nominee DOB, clear it.
    nominee_dob = _extract_first_labeled_dob(nominee_text)
    if nominee_dob:
        current_dob = _clean_text_field(enriched.get("dob"))
        if current_dob:
            current_norm = _normalize_date_only_string(current_dob)
            nominee_norm = _normalize_date_only_string(nominee_dob)
            if current_norm and nominee_norm and current_norm == nominee_norm:
                enriched["dob"] = ""

    return enriched


# Fields where a trailing "/<id number>" or similar glued-on code is a known
# extraction artifact (e.g. "MRS SAMPLE NAME/12345678901", the digits are a
# customer/mobile ID printed next to the name in the source document, not
# part of the name). Stripped defensively regardless of what the prompt asks
# for, since prompt instructions alone don't reliably prevent this.
_NAME_FIELDS_STRIP_ID_SUFFIX = {"customer_name"}
_TRAILING_ID_SUFFIX_RE = re.compile(r"\s*[/#]\s*\d{5,}\s*$")


def _strip_trailing_id_suffix(value: str) -> str:
    return _TRAILING_ID_SUFFIX_RE.sub("", value).strip()


# Fields where the model sometimes returns a numbered-list marker glued onto
# the front of a name (e.g. "1. John Doe") when it found multiple candidates
# in the source document. The marker isn't part of the name.
_NAME_FIELDS_STRIP_LIST_PREFIX = {"customer_name", "insurer"}
_LEADING_LIST_MARKER_RE = re.compile(r"^\s*\d+[.)]\s+")


def _strip_leading_list_marker(value: str) -> str:
    return _LEADING_LIST_MARKER_RE.sub("", value).strip()


# policy_type: for Zuno, keep only the part after the last em-dash / en-dash
# so "Zuno SmartDrive Motor Policy – Private Car package Insurance"
# becomes "Private Car package Insurance".
# For HDFC and other insurers the full name is already the correct value,
# so we only strip when the prefix is explicitly a Zuno product header.
_POLICY_TYPE_DASH_RE = re.compile(r"^.+[–—-]\s*(.+)$")
_ZUNO_HEADER_RE = re.compile(r"^Zuno\s+SmartDrive\s+Motor\s+Policy", re.IGNORECASE)


def _normalize_policy_type(value: str, is_zuno: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        return text
    # Only strip prefix for Zuno-style "Product – Type" headers
    if is_zuno or _ZUNO_HEADER_RE.match(text):
        match = _POLICY_TYPE_DASH_RE.match(text)
        if match:
            return match.group(1).strip()
    return text


def _normalize_vehicle_number(value: str) -> str:
    """Remove all whitespace and hyphens and convert to uppercase so 'DL-03-CCR-1234'
    becomes 'DL03CCR1234', matching standard Indian registration format."""
    text = str(value or "").strip()
    if not text:
        return text
    return re.sub(r"[\s\-]+", "", text).upper()


AMOUNT_FIELDS = {
    "idv",
    "od_net_premium",
    "tp_net_premium",
    "gst",
    "gross_premium",
    "net_premium",
}


def _normalize_amount(value: str) -> str:
    """Strip rupee symbol, commas, and surrounding whitespace from monetary
    values so '₹ 12,821.00' becomes '12821.00'."""
    text = str(value or "").strip()
    if not text:
        return text
    # Remove ₹, Rs., RS, commas, and any surrounding spaces
    text = re.sub(r"[₹\s]", "", text)      # drop ₹ and whitespace first
    text = re.sub(r"(?i)^rs\.?\s*", "", text)  # drop leading Rs / Rs.
    text = text.replace(",", "")           # drop thousand separators
    return text.strip()


def _normalize_date_only_string(value: str) -> str:
    raw_str = str(value or "").strip()
    if not raw_str:
        return ""

    # Keep only the date portion when the model emits a datetime.
    raw_str = re.sub(r"[T\s]+\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:\s*[APMapm]{2})?(?:\s*[A-Z]{2,5})?$", "", raw_str).strip()

    # Try natural text date formats first (e.g. "Jul 06, 2026", "08 Jul 2026", "Jun 20 2024")
    for fmt in (
        "%b %d, %Y",
        "%B %d, %Y",
        "%b %d %Y",
        "%B %d %Y",
        "%d %b %Y",
        "%d %B %Y",
        "%d-%b-%Y",
        "%d-%b-%y",
    ):
        try:
            return datetime.strptime(raw_str, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue

    text = raw_str.replace(".", "/").replace("-", "/")

    for fmt in (
        "%d/%m/%Y",
        "%d/%m/%y",
        "%Y/%m/%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
    ):
        try:
            return datetime.strptime(text, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue

    match = re.search(r"\b(\d{2})[./-](\d{2})[./-](\d{4})\b", text)
    if match:
        day, month, year = match.groups()
        return f"{day}/{month}/{year}"

    match = re.search(r"\b(\d{4})[./-](\d{2})[./-](\d{2})\b", text)
    if match:
        year, month, day = match.groups()
        return f"{day}/{month}/{year}"

    return raw_str


def validate_extracted_result(result: dict) -> dict:
    if not isinstance(result, dict):
        return result

    cleaned = dict(result)
    critical_fields = [
        "policy_number",
        "customer_name",
        "insurer",
        "policy_type",
        "vehicle_number",
        "policy_start",
        "policy_end",
    ]

    for field in list(cleaned.keys()):
        if field in {"file_name", "File Name"}:
            cleaned.pop(field, None)
        else:
            value = _clean_text_field(cleaned.get(field))
            if field in DATE_ONLY_FIELDS:
                value = _normalize_date_only_string(value)
            if field == "policy_type":
                value = _normalize_policy_type(value)
            if field == "policy_number":
                # Policy numbers never contain spaces: strip any that crept in
                # (e.g. HDFC prints them as "1111 2222 3333 4444 000")
                value = re.sub(r'\s+', '', value)
            if field == "vehicle_number":
                value = _normalize_vehicle_number(value)
            if field == "cpa":
                # Normalize CPA to Yes/No format
                value_lower = value.lower()
                if value_lower in ("yes", "y", "true", "1"):
                    value = "Yes"
                elif value_lower in ("no", "n", "false", "0", ""):
                    value = "No"
                elif value and value.replace(",", "").replace(".", "").isdigit():
                    # If it's a numeric amount, convert to Yes/No
                    amount = float(value.replace(",", ""))
                    value = "Yes" if amount > 0 else "No"
            if field in AMOUNT_FIELDS:
                value = _normalize_amount(value)
            if field in _NAME_FIELDS_STRIP_LIST_PREFIX:
                value = _strip_leading_list_marker(value)
            if field in _NAME_FIELDS_STRIP_ID_SUFFIX:
                value = _strip_trailing_id_suffix(value)
            # Reject boilerplate insurer support emails the AI may have picked
            # up from the document footer (e.g. support@hizuno.com).
            if field == "email" and value:
                domain = value.split("@")[-1].lower() if "@" in value else ""
                if domain in _BOILERPLATE_EMAIL_DOMAINS:
                    value = ""
            cleaned[field] = value

    dob_value = _clean_text_field(cleaned.get("dob"))
    if dob_value and not _is_valid_dob_year(dob_value):
        cleaned["dob"] = ""

    # Enforce: cheque_number and bank_name must only be populated for cheque payments.
    # Clear them unconditionally whenever payment_mode is anything other than "Cheque".
    payment_mode_val = str(cleaned.get("payment_mode") or "").strip().lower()
    if payment_mode_val != "cheque":
        cleaned["cheque_number"] = ""
        cleaned["bank_name"] = ""

    missing_critical = [field for field in critical_fields if not str(cleaned.get(field) or "").strip()]
    ambiguous = [
        field for field in critical_fields
        if field in cleaned and isinstance(cleaned.get(field), str) and any(marker in cleaned[field].lower() for marker in ("not present", "missing", "n/a", "null", "none"))
    ]

    if missing_critical or ambiguous:
        cleaned["needs_review"] = True
        cleaned["review_reason"] = "; ".join(
            [f"Critical fields missing: {', '.join(missing_critical)}" if missing_critical else "",
             f"Ambiguous values found in: {', '.join(ambiguous)}" if ambiguous else ""]
        ).strip("; ")
    else:
        cleaned["needs_review"] = False
        cleaned["review_reason"] = None

    return cleaned


def _extract_response_text(body: dict) -> str:
    if isinstance(body.get("output"), list):
        for item in body["output"]:
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        return content.get("text", "")
            if item.get("type") == "output_text":
                return item.get("text", "")
    text_field = body.get("text")
    if isinstance(text_field, dict):
        return text_field.get("text", "")
    if isinstance(text_field, str):
        return text_field
    if isinstance(body.get("choices"), list):
        try:
            return body["choices"][0]["message"]["content"]
        except Exception:
            pass
    return ""


def _split_policy_text_into_chunks(policy_text: str, max_chars: int) -> list[str]:
    text = str(policy_text or "").strip()
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return [text[:max_chars]]

    chunks: list[str] = []
    current_chunk: list[str] = []
    current_len = 0

    for paragraph in paragraphs:
        paragraph_len = len(paragraph)
        if paragraph_len > max_chars:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_len = 0
            for start in range(0, paragraph_len, max_chars):
                chunks.append(paragraph[start:start + max_chars])
            continue

        if current_len + paragraph_len + 2 <= max_chars:
            current_chunk.append(paragraph)
            current_len += paragraph_len + 2
        else:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
            current_chunk = [paragraph]
            current_len = paragraph_len

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks or [text[:max_chars]]


def _merge_chunk_results(base: dict, chunk_result: dict) -> dict:
    """Merge a later chunk's extraction result into the running accumulator.

    Fields where the base already has a value use "better wins" logic:
      - A longer, more specific value from a later chunk replaces a shorter
        partial one (e.g. 'MARUTI WAGON R 1.0' beats 'MARUTI').
      - For critical fields, a non-null value always beats null/blank.
    Pure accumulator fields (review_reason, exclusions) are concatenated.
    """
    if not isinstance(chunk_result, dict):
        return base

    merged = dict(base)
    for key, value in chunk_result.items():
        if key in {"error", "raw_output", "_debug_raw_model_output"}:
            continue
        if value is None:
            continue
        value_text = str(value).strip()
        if not value_text:
            continue

        if key == "needs_review":
            merged[key] = bool(merged.get(key) or value)
            continue

        if key == "review_reason":
            existing = str(merged.get(key) or "").strip()
            merged[key] = f"{existing}; {value_text}" if existing else value_text
            continue

        if key == "exclusions":
            existing = str(merged.get(key) or "").strip()
            merged[key] = f"{existing}; {value_text}" if existing else value_text
            continue

        existing_text = str(merged.get(key) or "").strip()
        if not existing_text:
            # Base has nothing: always take the new value.
            merged[key] = value
        elif len(value_text) > len(existing_text) + 2:
            # Later chunk returned a meaningfully longer (more complete) value;
            # prefer it unless it looks like boilerplate noise.
            merged[key] = value
        # Otherwise keep the existing value.

    return merged


def _get_missing_critical_fields(result: dict) -> list[tuple[str, str]]:
    critical_fields = [
        ("policy_number",   "Policy Number"),
        ("customer_name",   "Customer Name"),
        ("insurer",         "Insurer"),
        ("policy_type",     "Policy Type"),
        ("vehicle_number",  "Vehicle Number"),
        ("policy_start",    "Policy Start Date"),
        ("policy_end",      "Policy End Date"),
    ]
    missing = []
    for field_key, field_label in critical_fields:
        value = result.get(field_key)
        if value is None or str(value).strip() in {"", "null", "none", "n/a"}:
            missing.append((field_key, field_label))
    return missing


def _focus_prompt_for_missing_fields(
    policy_text: str, missing_fields: list[tuple[str, str]], company_mode: str | None = None
) -> str:
    focused_fields = [
        (field_key, field_label)
        for field_key, field_label in missing_fields
        if field_key not in {"file_name", "filename", "file"}
    ]
    if not focused_fields:
        return ""
    return make_extraction_prompt(policy_text, focused_fields, company_mode=company_mode)


def _run_inference_chunk(
    api_key: str,
    api_base_url: str,
    model_name: str,
    chunk_text: str,
    log_placeholder,
    azure_api_version: str = "",
    request_index: int = 1,
    fields: list[tuple[str, str]] | None = None,
    company_mode: str | None = None,
    _session: requests.Session | None = None,
) -> dict:
    prompt = make_extraction_prompt(chunk_text, fields or POLICY_FIELDS, company_mode=company_mode)
    log_lines = [f"Sending chunk {request_index} to {model_name} via cloud API..."]
    _safe_log(log_placeholder, 
        f'<div class="progress-log">{chr(10).join(log_lines)}</div>',
        unsafe_allow_html=True,
    )

    is_azure_endpoint = bool(azure_api_version) or ".azure.com" in api_base_url.lower()
    is_foundry = ".services.ai.azure.com" in api_base_url.lower()
    is_responses_endpoint = is_foundry or "/openai/v1/responses" in api_base_url.lower()

    if is_azure_endpoint:
        if not model_name:
            return {"error": "Azure deployment name is required."}
        headers = {
            "api-key": api_key,
            "Content-Type": "application/json",
        }
        if is_responses_endpoint:
            request_url = api_base_url.rstrip("/")
            if not request_url.lower().endswith("/openai/v1/responses"):
                request_url = f"{request_url}/openai/v1/responses"
        elif is_foundry:
            request_url = f"{api_base_url.rstrip('/')}/openai/v1/chat/completions"
        else:
            azure_api_version = azure_api_version or DEFAULT_AZURE_API_VERSION
            request_url = (
                f"{api_base_url.rstrip('/')}/openai/deployments/"
                f"{model_name}/chat/completions?api-version={azure_api_version}"
            )
    else:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        request_url = f"{api_base_url.rstrip('/')}/chat/completions"

    if is_responses_endpoint:
        schema_fields = fields or POLICY_FIELDS
        payload = {
            "model": model_name,
            "input": prompt,
            "max_output_tokens": 4096,
            # Reasoning models (gpt-5 family) spend part of max_output_tokens
            # on hidden reasoning before producing visible output. "medium"
            # gives the model enough room to reason carefully about dense,
            # inconsistently-formatted insurance wording. We previously
            # lowered this to save tokens, but with the 8000 token budget
            # and strict schema now in place that's no longer necessary;
            # and a lower effort was likely why fields that ARE in the
            # document started coming back null.
            "reasoning": {"effort": "low"},
            # Strict structured output: guarantees valid JSON with exactly
            # the keys we asked for, so there's no "invalid JSON" failure
            # mode and no risk of missing keys downstream.
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "policy_extraction",
                    "schema": build_json_schema(schema_fields),
                    "strict": True,
                }
            },
        }
    else:
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4096, 
            "response_format": {"type": "json_object"}
        }
        if not is_foundry:
            payload["temperature"] = 0.0

    try:
        t0 = time.perf_counter()
        _is_mocked = getattr(requests.post, "__module__", "") != "requests.api"
        _post = requests.post if (_is_mocked or _session is None) else _session.post
        post_kwargs = {
            "headers": headers,
            "json": payload,
            "timeout": 180,
        }
        if not _is_mocked:
            post_kwargs["verify"] = SSL_VERIFY
        response = _post(request_url, **post_kwargs)
        response.raise_for_status()
        elapsed = time.perf_counter() - t0
        body = response.json()
        raw_output = _extract_response_text(body)
        if not raw_output and not is_responses_endpoint:
            raw_output = body["choices"][0]["message"]["content"]
        log_lines[-1] = f"Chunk {request_index} completed in {elapsed:.1f}s"
        _safe_log(log_placeholder, 
            f'<div class="progress-log">{chr(10).join(log_lines)}</div>',
            unsafe_allow_html=True,
        )
        parsed = _parse_model_json(raw_output)
        if isinstance(parsed, dict) and "error" not in parsed:
            parsed["_debug_raw_model_output"] = raw_output
            return parsed
        fallback = _fallback_extract_fields(chunk_text, company_mode=company_mode)
        fallback["_debug_raw_model_output"] = raw_output
        if isinstance(parsed, dict) and "error" in parsed:
            fallback["review_reason"] = parsed["error"]
            fallback["needs_review"] = True
        return fallback
    except requests.exceptions.HTTPError as exc:
        if exc.response is None:
            return {"error": f"API call failed: {exc}"}

        error_detail = ""
        try:
            err_body = exc.response.json()
            inner = err_body.get("error", {})
            error_detail = inner.get("message", "") or str(err_body)
        except Exception:
            error_detail = exc.response.text[:500] if exc.response.text else ""

        if is_azure_endpoint and exc.response.status_code == 404:
            return {
                "error": (
                    f"Azure deployment '{model_name}' was not found at '{api_base_url}'. "
                    f"Please enter the exact Deployment Name configured in your Azure OpenAI Studio."
                    + (f"\n\nAzure says: {error_detail}" if error_detail else "")
                )
            }
        if error_detail:
            return {"error": f"API call failed ({exc.response.status_code}): {error_detail}"}
        return {"error": f"API call failed: {exc}"}
    except Exception as exc:
        return {"error": f"API call failed: {exc}"}


def run_inference_api(
    api_key: str,
    api_base_url: str,
    model_name: str,
    policy_text: str,
    max_chars: int | None,
    log_placeholder,
    azure_api_version: str = "",
    company_mode: str | None = None,
) -> dict:
    """Call an OpenAI-compatible or Azure OpenAI chat endpoint."""
    # One session per inference run: reuses the TCP/TLS connection across
    # all chunk calls (follow-up included), avoiding a fresh handshake each time.
    _session = requests.Session()

    if max_chars is None:
        log_lines: list[str] = [f"Sending full document to {model_name} via cloud API..."]
        _safe_log(log_placeholder, 
            f'<div class="progress-log">{chr(10).join(log_lines)}</div>',
            unsafe_allow_html=True,
        )

        chunk_result = _run_inference_chunk(
            api_key=api_key,
            api_base_url=api_base_url,
            model_name=model_name,
            chunk_text=policy_text,
            log_placeholder=log_placeholder,
            azure_api_version=azure_api_version,
            request_index=1,
            company_mode=company_mode,
            _session=_session,
        )

        if isinstance(chunk_result, dict) and "error" in chunk_result:
            err_msg = chunk_result.get("error", "AI extraction error")
            _safe_log(log_placeholder, f"{err_msg}, using deterministic fallback")
            fb = _fallback_extract_fields(policy_text, company_mode=company_mode)
            fb = _backfill_missing_fields_from_text(fb, policy_text, company_mode=company_mode)
            fb = validate_extracted_result(fb)
            fb["needs_review"] = True
            fb["review_reason"] = err_msg
            return fb

        # Only fall back to the crude regex scrape when the model genuinely
        # returned nothing usable (e.g. empty dict, or every value blank).
        # Previously the regex fallback was used as the base every time,
        # which meant a correct AI "null" for a field it couldn't find was
        # silently overwritten with a mid-sentence text fragment grabbed by
        # regex; that's what produced garbage values like "d herein".
        model_has_content = isinstance(chunk_result, dict) and any(
            str(v).strip() for v in chunk_result.values() if v is not None
        )
        if model_has_content:
            merged = dict(chunk_result)
        else:
            merged = _fallback_extract_fields(policy_text, company_mode=company_mode)
            merged = _merge_chunk_results(merged, chunk_result)
        merged = _backfill_missing_fields_from_text(merged, policy_text, company_mode=company_mode)
        merged = validate_extracted_result(merged)
        if not model_has_content:
            merged["needs_review"] = True
            merged["review_reason"] = (
                "AI extraction returned no usable content; fields below are from "
                "a basic text-pattern scrape, not the AI model, and should be "
                "checked manually."
            )

        missing_fields = _get_missing_critical_fields(merged)
        if missing_fields:
            focus_prompt = _focus_prompt_for_missing_fields(policy_text, missing_fields, company_mode=company_mode)
            if focus_prompt:
                follow_up = _run_inference_chunk(
                    api_key=api_key,
                    api_base_url=api_base_url,
                    model_name=model_name,
                    chunk_text=policy_text,
                    log_placeholder=log_placeholder,
                    azure_api_version=azure_api_version,
                    request_index=2,
                    fields=missing_fields,
                    company_mode=company_mode,
                    _session=_session,
                )
                if isinstance(follow_up, dict) and "error" not in follow_up:
                    merged = _merge_chunk_results(merged, follow_up)
                    merged = _backfill_missing_fields_from_text(merged, policy_text, company_mode=company_mode)
                    merged = validate_extracted_result(merged)

        return merged

    chunks = _split_policy_text_into_chunks(policy_text, max_chars=max_chars)
    if not chunks:
        return _fallback_extract_fields(policy_text, company_mode=company_mode)

    log_lines: list[str] = [f"Sending to {model_name} via cloud API..."]
    _safe_log(log_placeholder, 
        f'<div class="progress-log">{chr(10).join(log_lines)}</div>',
        unsafe_allow_html=True,
    )

    fallback_seed = _fallback_extract_fields(policy_text, company_mode=company_mode)
    merged: dict = {}
    any_model_content = False
    chunk_results: list[dict] = []
    for idx, chunk in enumerate(chunks, 1):
        chunk_result = _run_inference_chunk(
            api_key=api_key,
            api_base_url=api_base_url,
            model_name=model_name,
            chunk_text=chunk,
            log_placeholder=log_placeholder,
            azure_api_version=azure_api_version,
            request_index=idx,
            company_mode=company_mode,
            _session=_session,
        )

        if isinstance(chunk_result, dict) and "error" in chunk_result:
            return chunk_result

        if isinstance(chunk_result, dict) and any(
            str(v).strip() for v in chunk_result.values() if v is not None
        ):
            any_model_content = True

        chunk_results.append(chunk_result)
        merged = _merge_chunk_results(merged, chunk_result)

    # Only patch in the crude regex scrape if the model returned nothing
    # usable across every chunk: never let it override a genuine model
    # result (see single-chunk path above for why).
    if not any_model_content:
        merged = dict(fallback_seed)
        for chunk_result in chunk_results:
            merged = _merge_chunk_results(merged, chunk_result)

    merged = _backfill_missing_fields_from_text(merged, policy_text, company_mode=company_mode)
    merged = validate_extracted_result(merged)
    missing_fields = _get_missing_critical_fields(merged)
    if missing_fields:
        focus_prompt = _focus_prompt_for_missing_fields(policy_text, missing_fields, company_mode=company_mode)
        if focus_prompt:
            follow_up = _run_inference_chunk(
                api_key=api_key,
                api_base_url=api_base_url,
                model_name=model_name,
                chunk_text=policy_text,
                log_placeholder=log_placeholder,
                azure_api_version=azure_api_version,
                request_index=len(chunks) + 1,
                fields=missing_fields,
                company_mode=company_mode,
                _session=_session,
            )
            if isinstance(follow_up, dict) and "error" not in follow_up:
                merged = _merge_chunk_results(merged, follow_up)
                merged = _backfill_missing_fields_from_text(merged, policy_text, company_mode=company_mode)
                merged = validate_extracted_result(merged)

    return merged


# ─────────────────────────────────────────────────────────────────────────────
# Result card renderer
# ─────────────────────────────────────────────────────────────────────────────

def _row_exists_in_workbook(output_path: Path, sheet_name: str, filename: str) -> bool:
    try:
        from openpyxl import load_workbook

        wb = load_workbook(output_path, read_only=True, data_only=True)
        ws = wb[sheet_name] if sheet_name in wb.sheetnames else wb.active
        if ws is None:
            return False

        # Locate the file-name column by header instead of assuming column 1,
        # since the workbook layout can vary (e.g. extra leading columns).
        file_name_col = 1
        for col in range(1, ws.max_column + 1):
            header = ws.cell(row=1, column=col).value
            normalized = re.sub(r"[^a-z0-9]+", "", str(header or "").strip().lower())
            if normalized in {"filename", "file_name", "file"}:
                file_name_col = col
                break

        for row_idx in range(2, ws.max_row + 1):
            if str(ws.cell(row=row_idx, column=file_name_col).value or "") == filename:
                return True
        return False
    except Exception:
        return False
    finally:
        try:
            wb.close()
        except Exception:
            pass


def append_result_to_excel(filename: str, data: dict, output_excel: str | os.PathLike | None = None, sheet_name: str = "Policies") -> Path:
    output_path = Path(output_excel or DEFAULT_OUTPUT_EXCEL)
    row_data = {"File Name": filename}

    if isinstance(data, dict):
        for key, value in data.items():
            if key in {"error", "raw_output", "page_count", "warnings"}:
                row_data[key] = value if value is not None else ""
            else:
                # Use 0 for numeric fields, empty string for text fields
                if value is None:
                    row_data[key] = 0 if key in NUMERIC_FIELDS else ""
                else:
                    row_data[key] = value

    row_data = _sanitize_export_row_data(row_data)

    # Retry up to 5 times with a short delay before giving up on the primary file.
    last_exc = None
    for attempt in range(5):
        try:
            _append_row_to_excel(output_path, sheet_name=sheet_name, row_data=row_data)
            format_excel_file(output_path, sheet_name=sheet_name)
            if not _row_exists_in_workbook(output_path, sheet_name=sheet_name, filename=filename):
                raise RuntimeError("Workbook write did not persist the row data.")
            return output_path
        except PermissionError as exc:
            last_exc = exc
            time.sleep(2)  # wait 2s for Excel to release the file lock, then retry
        except Exception as exc:
            last_exc = exc
            time.sleep(1)

    # All retries exhausted: only NOW create a timestamped fallback.
    fallback_name = f"{output_path.stem}_LOCKED_{datetime.now().strftime('%Y%m%d_%H%M%S')}{output_path.suffix}"
    fallback_path = output_path.with_name(fallback_name)
    _append_row_to_excel(fallback_path, sheet_name=sheet_name, row_data=row_data)
    format_excel_file(fallback_path, sheet_name=sheet_name)
    logger.warning(f"Could not write to {output_path.name} after 5 attempts ({last_exc}). Saved to {fallback_path.name} instead.")
    return fallback_path



# Backward compatibility alias
CANONICAL_POLICY_FIELDS = POLICY_FIELDS
