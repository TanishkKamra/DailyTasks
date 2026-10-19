#!/usr/bin/env python3
"""
ocr_core.py, PAN card field extractor.

Reads PAN card photos and extracts PAN number, name (candidate only),
and date of birth. Uses a best-effort approach, always returns whatever
it found, never silently drops results.

Usage:
    python3 ocr_core.py card.jpg
    python3 ocr_core.py card1.jpg card2.png ./folder_of_cards/
    python3 ocr_core.py ./cards/ --json results.json

Requirements (install once):
    pip install opencv-python-headless pytesseract pillow numpy
    + the tesseract-ocr binary itself (e.g. `apt install tesseract-ocr`
      on Debian/Ubuntu, `brew install tesseract` on Mac).
"""
import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
try:
    import pytesseract
except ImportError:
    pytesseract = None
from PIL import Image, ImageOps

try:
    import pymupdf as fitz  # PyMuPDF
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".pdf"}


def pdf_to_images(pdf_bytes: bytes) -> List[bytes]:
    """Convert each page of a PDF into JPEG bytes using PyMuPDF."""
    if fitz is None:
        return []
    images: List[bytes] = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page in doc:
            pix = page.get_pixmap(dpi=250)
            images.append(pix.tobytes("jpeg"))
        doc.close()
    except Exception:
        pass
    return images

# ===========================================================================
# 1. Loading + orientation
# ===========================================================================

HEADER_KEYWORDS = ("income tax", "govt", "permanent account")


def load_and_orient(source) -> np.ndarray:
    pil_img = ImageOps.exif_transpose(Image.open(source).convert("RGB"))
    image = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    proxy = _downscale(image, target_width=800)
    best_k, best_score = 0, _header_score(proxy)
    for k in (1, 2, 3):
        score = _header_score(np.rot90(proxy, k=k))
        if score > best_score:
            best_k, best_score = k, score
    return np.rot90(image, k=best_k) if best_k else image


def _downscale(image: np.ndarray, target_width: int) -> np.ndarray:
    h, w = image.shape[:2]
    if w <= target_width:
        return image
    scale = target_width / w
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


def _header_score(image: np.ndarray) -> int:
    tokens = run_ocr(image)
    text = " ".join(t.text for t in tokens).lower()
    return sum(kw in text for kw in HEADER_KEYWORDS)


# ===========================================================================
# 2. Preprocessing
# ===========================================================================

def preprocess(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    if w < 1000:
        scale = 1000 / w
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    elif w > 1800:
        scale = 1800 / w
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = cv2.fastNlMeansDenoising(gray, h=10)
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    return gray


# ===========================================================================
# 3. OCR
# ===========================================================================

@dataclass
class Token:
    text: str
    conf: float
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


_DEVANAGARI_DIGITS = {chr(0x0966 + i): str(i) for i in range(10)}


def _normalize_digits(text: str) -> str:
    return "".join(_DEVANAGARI_DIGITS.get(ch, ch) for ch in text)


try:
    from rapidocr_onnxruntime import RapidOCR
    _rapid_engine = RapidOCR()
    OCR_ENGINE = "rapidocr"
except Exception:
    _rapid_engine = None
    # Only claim "tesseract" if pytesseract actually imported; otherwise
    # neither engine is usable and we must say so instead of letting
    # _run_tesseract() crash later with a confusing
    # "'NoneType' object has no attribute 'image_to_data'".
    OCR_ENGINE = "tesseract" if pytesseract is not None else "none"


def run_ocr(image: np.ndarray) -> List[Token]:
    if OCR_ENGINE == "rapidocr":
        return _run_rapidocr(image)
    if OCR_ENGINE == "tesseract":
        return _run_tesseract(image)
    raise RuntimeError(
        "No OCR engine is available: rapidocr_onnxruntime failed to load and "
        "pytesseract is not installed. Install one of them, e.g. "
        "`pip install rapidocr-onnxruntime` or `pip install pytesseract` "
        "plus the tesseract-ocr binary, then restart the server."
    )


def _run_rapidocr(image: np.ndarray) -> List[Token]:
    result, _ = _rapid_engine(image)
    tokens = []
    if result:
        for box, text, conf in result:
            text = _normalize_digits(text.strip())
            if not text:
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            tokens.append(Token(
                text=text, conf=float(conf),
                x=min(xs), y=min(ys), w=max(xs) - min(xs), h=max(ys) - min(ys),
            ))
    return tokens


def _run_tesseract(image: np.ndarray) -> List[Token]:
    tokens = []
    for psm in ("--psm 11", "--psm 6"):
        data = pytesseract.image_to_data(image, config=psm, output_type=pytesseract.Output.DICT)
        seen_texts = {t.text for t in tokens}
        for i, raw_text in enumerate(data["text"]):
            text = _normalize_digits(raw_text.strip())
            conf = float(data["conf"][i])
            if not text or conf < 0:
                continue
            if text in seen_texts:
                continue  # deduplicate
            tokens.append(Token(
                text=text, conf=conf / 100.0,
                x=float(data["left"][i]), y=float(data["top"][i]),
                w=float(data["width"][i]), h=float(data["height"][i]),
            ))
    return tokens


# ===========================================================================
# 4. Field extraction
# ===========================================================================

PAN_REGEX = re.compile(r"[A-Z]{5}[0-9]{4}[A-Z]")
PAN_STRICT_REGEX = re.compile(r"^[A-Z]{3}[PCHFATBLJG][A-Z][0-9]{4}[A-Z]$")
DATE_REGEX = re.compile(r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}")

# Labels that mark non-name lines: used to skip them during name search
SKIP_PATTERNS = (
    r"income\s*tax",
    r"govt",
    r"government",
    r"permanent\s*account",
    r"aaykar",
    r"bharat",
    r"sarkar",
    r"vibhag",
    r"signature",
    r"date\s*of\s*birth",
    r"\bdob\b",
    r"father",
    r"\bs/o\b",
    r"\bf/o\b",
)

DOB_LABEL_PATTERNS    = (r"date\s*of\s*birth", r"\bdob\b")
FATHER_LABEL_PATTERNS = (r"father", r"father.s\s*name", r"\bs/o\b", r"\bf/o\b", r"\bpita\b")
NAME_LABEL_PATTERNS   = (r"^name$",)

_DIGIT_TO_LETTER = {"0": "O", "1": "I", "5": "S", "8": "B"}
_LETTER_TO_DIGIT = {"O": "0", "I": "1", "S": "5", "B": "8", "Z": "2"}


def _is_skip_label(text: str) -> bool:
    return any(re.search(p, text.lower()) for p in SKIP_PATTERNS)


def _is_dob_label(text: str) -> bool:
    return any(re.search(p, text.lower()) for p in DOB_LABEL_PATTERNS)


def _is_name_label(text: str) -> bool:
    """
    Match ONLY the standalone label "Name" printed on the PAN card.
    Must NOT match "Father's Name", "Father Name", etc.
    """
    t = text.lower().strip()
    # Immediately reject if the token contains "father" or known father-label words
    if _is_father_label(text):
        return False
    return any(re.search(p, t) for p in NAME_LABEL_PATTERNS)

def _is_father_label(text: str) -> bool:
    return any(re.search(p, text.lower()) for p in FATHER_LABEL_PATTERNS)

def _correct_pan_confusables(candidate: str) -> str:
    if len(candidate) != 10:
        return candidate
    chars = list(candidate)
    for i, ch in enumerate(chars):
        if (i < 5 or i == 9) and ch in _DIGIT_TO_LETTER:
            chars[i] = _DIGIT_TO_LETTER[ch]
        elif 5 <= i < 9 and ch in _LETTER_TO_DIGIT:
            chars[i] = _LETTER_TO_DIGIT[ch]
    return "".join(chars)


def _clean_alphanum(text: str) -> str:
    """Strip everything except A-Z and 0-9."""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def find_pan(tokens: List[Token]) -> Tuple[Optional[str], float]:
    """
    Multi-strategy PAN extraction, tries progressively looser methods so
    a single bad token never causes a miss.

    Strategy 1, single token exact match (highest confidence)
    Strategy 2, single token after confusable correction
    Strategy 3, regex scan across concatenated clean text of all tokens
    Strategy 4, sliding-window across tokens sorted by position
                 (handles OCR splitting a PAN across 2 tokens, e.g. "EMXPK" + "8369H")
    Strategy 5, character-level sliding window on full concatenated raw text
                 (catches noise characters injected between PAN characters)
    """
    # Pre-clean each token once
    cleaned_tokens = [(t, _clean_alphanum(t.text)) for t in tokens]

    # --- Strategy 1 & 2: single token ---
    for t, clean in cleaned_tokens:
        if PAN_REGEX.fullmatch(clean):
            return clean, t.conf
    for t, clean in cleaned_tokens:
        corrected = _correct_pan_confusables(clean)
        if PAN_REGEX.fullmatch(corrected):
            return corrected, t.conf

    # --- Strategy 3: regex on joined clean text ---
    joined_clean = "".join(c for _, c in cleaned_tokens)
    m = PAN_REGEX.search(joined_clean)
    if m:
        value = m.group(0)
        # Assign confidence from the token most likely containing it
        for t, clean in cleaned_tokens:
            if clean and value[2:7] in clean:   # match on the distinctive middle chars
                return value, t.conf
        return value, 0.5  # found but can't pin to a token

    # --- Strategy 4: sliding window across adjacent tokens (sorted by x,y) ---
    # Sort by reading order (top-to-bottom, left-to-right)
    sorted_tokens = sorted(cleaned_tokens, key=lambda tc: (round(tc[0].cy / 20), tc[0].cx))
    for window in range(2, 5):  # try joining 2, 3, 4 adjacent tokens
        for i in range(len(sorted_tokens) - window + 1):
            chunk = "".join(c for _, c in sorted_tokens[i:i+window])
            # Look for a 10-char alphanum substring
            for start in range(len(chunk) - 9):
                candidate = chunk[start:start+10]
                corrected = _correct_pan_confusables(candidate)
                if PAN_REGEX.fullmatch(corrected):
                    avg_conf = sum(t.conf for t, _ in sorted_tokens[i:i+window]) / window
                    return corrected, avg_conf

    # --- Strategy 5: character-level scan on raw concatenated text ---
    # Some OCR engines inject spaces or punctuation inside a PAN
    raw_joined = re.sub(r"[^A-Z0-9]", "", "".join(t.text.upper() for t in tokens))
    for start in range(len(raw_joined) - 9):
        candidate = raw_joined[start:start+10]
        corrected = _correct_pan_confusables(candidate)
        if PAN_STRICT_REGEX.match(corrected):
            return corrected, 0.3  # low confidence, extracted from noise

    return None, 0.0


def _is_name_like(text: str, strict: bool = True) -> bool:
    """
    A name token should be mostly uppercase letters and spaces.
    strict=True  : original thresholds (used in positional fallback)
    strict=False : relaxed thresholds  (used in label-anchored strategies)
    """
    cleaned = text.strip()
    if len(cleaned) < 2:
        return False
    if re.search(r"\d", cleaned):
        return False
    if _is_skip_label(cleaned):
        return False
    alpha_ratio = sum(c.isalpha() or c in " .-'" for c in cleaned) / len(cleaned)
    min_alpha = 0.85 if strict else 0.70
    if alpha_ratio < min_alpha:
        return False
    letters = [c for c in cleaned if c.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(c.isupper() for c in letters) / len(letters)
    min_upper = 0.70 if strict else 0.45
    return upper_ratio >= min_upper


def extract_fields(tokens: List[Token], img_h: float, img_w: float) -> Tuple[Dict, Dict]:
    """
    Extract PAN, candidate name, and DOB from token list.

    Strategy:
    - PAN: regex scan across all tokens (with confusable correction).
    - DOB: look for a date-shaped token; prefer one near a DOB label.
    - Name: PAN cards have TWO name lines, candidate (line 1) then father (line 2).
      We find them positionally: sort name-like tokens by Y, pick the FIRST one
      (topmost) as the candidate name.
    """
    values = {"pan": None, "name": None, "dob": None}
    confs  = {"pan": 0.0,  "name": 0.0,  "dob": 0.0}
    used: set = set()

    # --- PAN ---
    pan, pan_conf = find_pan(tokens)
    values["pan"], confs["pan"] = pan, pan_conf
    pan_token_ids = set()
    for t in tokens:
        if pan and pan in t.text.upper().replace(" ", ""):
            used.add(id(t))
            pan_token_ids.add(id(t))

    # --- DOB ---
    # First try: token near a DOB label
    dob_assigned = False
    for t in sorted(tokens, key=lambda x: x.cy):
        if id(t) in used or not _is_dob_label(t.text):
            continue
        used.add(id(t))
        # Look for a date-like token just below it
        for candidate in sorted(tokens, key=lambda x: x.cy):
            if id(candidate) in used:
                continue
            v_dist = candidate.cy - t.cy
            h_dist = abs(candidate.cx - t.cx)
            if 0 < v_dist < img_h * 0.20 and h_dist < img_w * 0.7:
                m = DATE_REGEX.search(candidate.text)
                if m:
                    values["dob"] = m.group(0)
                    confs["dob"] = candidate.conf
                    used.add(id(candidate))
                    dob_assigned = True
                    break
        if dob_assigned:
            break

    # Fallback: any date-shaped token not already used
    if not dob_assigned:
        date_tokens = [t for t in tokens if id(t) not in used and DATE_REGEX.search(t.text)]
        if date_tokens:
            chosen = min(date_tokens, key=lambda t: t.cy)
            values["dob"] = DATE_REGEX.search(chosen.text).group(0)
            confs["dob"] = chosen.conf
            used.add(id(chosen))

    # --- Candidate Name ---
    # PAN card layout (top to bottom):
    #   [INCOME TAX DEPT header]
    #   [CANDIDATE NAME]     ← we want this
    #   [FATHER NAME]
    #   [Date of Birth]  [DOB value]
    #   [PAN number]
    #
    # We try 4 strategies in order of reliability.

    # ── shared helpers ────────────────────────────────────────────────────────
    def same_row(a: Token, b: Token) -> bool:
        return abs(a.cy - b.cy) < img_h * 0.05

    def build_rows(candidates: List[Token]) -> List[List[Token]]:
        """Group tokens into horizontal rows, sorted top-to-bottom."""
        cands = sorted(candidates, key=lambda t: t.cy)
        rows: List[List[Token]] = []
        for t in cands:
            placed = False
            for row in rows:
                if same_row(row[0], t):
                    row.append(t)
                    placed = True
                    break
            if not placed:
                rows.append([t])
        return rows

    def row_to_name(row: List[Token]) -> str:
        row = sorted(row, key=lambda t: t.cx)
        raw = " ".join(t.text for t in row).strip().upper()
        raw = re.sub(r"[^A-Z\s.\-\']", "", raw).strip()
        return re.sub(r"\s+", " ", raw)

    def set_name(row: List[Token], conf_override: float = None) -> None:
        name_str = row_to_name(row)
        if len(name_str) >= 2:
            avg_conf = conf_override or (sum(t.conf for t in row) / len(row))
            values["name"] = name_str
            confs["name"] = avg_conf
            for t in row:
                used.add(id(t))

    name_assigned = False

    # ── Strategy A: "Name" label anchor ──────────────────────────────────────
    # The word "Name" is printed on the card just above the CANDIDATE name.
    # We must find the FIRST (topmost) "Name" label that is NOT part of
    # "Father's Name", _is_name_label already guards this, but we also
    # sort by Y and take only the first qualifying label so we never
    # accidentally anchor on the father-name label row.
    name_label_tokens = [
        t for t in tokens
        if id(t) not in used and _is_name_label(t.text)
    ]
    # Sort top-to-bottom; the candidate "Name" label is always above
    # the "Father's Name" label on a PAN card
    name_label_tokens.sort(key=lambda t: t.cy)

    for label_tok in name_label_tokens:
        used.add(id(label_tok))
        # Collect name-like tokens DIRECTLY below this label only,
        # stopping before the next label row (father / dob)
        father_y = min(
            (t.cy for t in tokens if _is_father_label(t.text)),
            default=img_h
        )
        dob_label_y = min(
            (t.cy for t in tokens if _is_dob_label(t.text)),
            default=img_h
        )
        upper_bound = min(father_y, dob_label_y, label_tok.cy + img_h * 0.22)

        below = [
            t for t in tokens
            if id(t) not in used
            and label_tok.cy < t.cy < upper_bound
            and abs(t.cx - label_tok.cx) < img_w * 0.85
            and _is_name_like(t.text, strict=False)
        ]
        if below:
            rows = build_rows(below)
            set_name(rows[0])
            name_assigned = True
            break

    # ── Strategy B: Father label anchor ──────────────────────────────────────
    # "Father's Name" / "S/O" label is printed below the candidate name.
    # The row immediately ABOVE this label is the candidate name.
    # We exclude the father's NAME VALUE (the row just below the father label)
    # so it is never confused with the candidate name.
    if not name_assigned:
        father_labels = [
            t for t in sorted(tokens, key=lambda x: x.cy)
            if id(t) not in used and _is_father_label(t.text)
        ]
        if father_labels:
            father_t = father_labels[0]

            # Mark everything AT or BELOW the father label as off-limits
            father_value_tokens = {
                id(t) for t in tokens
                if t.cy >= father_t.cy and id(t) not in used
            }

            above = [
                t for t in tokens
                if id(t) not in used
                and id(t) not in father_value_tokens
                and 0 < (father_t.cy - t.cy) < img_h * 0.22
                and _is_name_like(t.text, strict=False)
            ]
            if above:
                rows = build_rows(above)
                # Take the LAST row (closest above the father label) = candidate name
                set_name(rows[-1])
                name_assigned = True

    # ── Strategy C: DOB-anchor, row just ABOVE the DOB ──────────────────────
    # DOB is always below the candidate name. Take name-like tokens in the
    # band between the header bottom and the DOB row.
    if not name_assigned:
        header_bottom_y = 0.0
        for t in tokens:
            if any(kw in t.text.lower() for kw in (
                "income", "tax", "dept", "vibhag", "aaykar",
                "bharat", "sarkar", "govt", "department"
            )):
                bottom_edge = t.y + t.h
                if bottom_edge > header_bottom_y:
                    header_bottom_y = bottom_edge

        dob_y = img_h
        if values["dob"]:
            for t in tokens:
                if DATE_REGEX.search(t.text):
                    dob_y = min(dob_y, t.cy)

        band = [
            t for t in tokens
            if id(t) not in used
            and t.cy > header_bottom_y
            and t.cy < dob_y
            and _is_name_like(t.text, strict=False)
        ]
        if band:
            rows = build_rows(band)
            set_name(rows[0])
            name_assigned = True

    # ── Strategy D: full-page fallback, any name-like token not yet used ─────
    # Last resort: grab every unused name-like token, group by row,
    # skip the topmost (likely a header fragment) and pick the next one.
    if not name_assigned:
        remaining = [
            t for t in tokens
            if id(t) not in used and _is_name_like(t.text, strict=False)
        ]
        if remaining:
            rows = build_rows(remaining)
            # Skip rows that look like header fragments (very short tokens)
            for row in rows:
                candidate_str = row_to_name(row)
                if len(candidate_str) >= 3:
                    set_name(row)
                    break

    return values, confs


# ===========================================================================
# 5. Normalization: best-effort, never rejects
# ===========================================================================

PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
VALID_CATEGORY_CODES = set("ABCFGHLJPTE")

DATE_FORMATS = [
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
    "%m/%d/%Y", "%m-%d-%Y",  # fallback for ambiguous dates
]


@dataclass
class Field:
    value: Optional[str]
    confidence: float
    valid: bool   # True = passed format check; False = could not verify but still returned


def normalize_pan(raw: Optional[str], conf: float) -> Field:
    if not raw:
        return Field(None, conf, False)
    cleaned = raw.upper().replace(" ", "")
    # Attempt confusable correction if not matching yet
    if not PAN_PATTERN.match(cleaned):
        cleaned = _correct_pan_confusables(cleaned)
    valid = bool(PAN_STRICT_REGEX.match(cleaned))
    # Return whatever we have, even if it doesn't fully validate
    return Field(cleaned, conf, valid)


def normalize_name(raw: Optional[str], conf: float) -> Field:
    if not raw:
        return Field(None, conf, False)
    cleaned = re.sub(r"[^A-Za-z\s.'\-]", "", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().upper()
    valid = len(cleaned) >= 3 and any(c.isalpha() for c in cleaned)
    return Field(cleaned or None, conf, valid)


def normalize_dob(raw: Optional[str], conf: float) -> Field:
    """
    Try to parse the date into a canonical YYYY-MM-DD form.
    If parsing fails, return the raw string as-is (still useful).
    Never returns None when we have a value, just marks valid=False if unparseable.
    """
    if not raw:
        return Field(None, conf, False)
    import datetime as _dt
    _current_year = _dt.date.today().year
    for fmt in DATE_FORMATS:
        try:
            from datetime import datetime
            parsed = datetime.strptime(raw.strip(), fmt)
            # Sanity: year must be plausible for a living PAN holder.
            # Upper bound is the current year so the check never goes stale.
            if 1900 <= parsed.year <= _current_year:
                return Field(parsed.strftime("%d/%m/%Y"), conf, True)
        except ValueError:
            continue
    # Parsing failed, return raw string, mark as unverified but not rejected
    return Field(raw.strip(), conf, False)


def normalize_all(values: Dict, confs: Dict) -> Dict[str, Field]:
    return {
        "pan":  normalize_pan(values["pan"],  confs["pan"]),
        "name": normalize_name(values["name"], confs["name"]),
        "dob":  normalize_dob(values["dob"],  confs["dob"]),
    }


# ===========================================================================
# 6. Pipeline + CLI
# ===========================================================================

def _run_pipeline(source) -> Dict[str, Field]:
    image = load_and_orient(source)
    processed = preprocess(image)
    h, w = processed.shape[:2]
    tokens = run_ocr(processed)
    raw_values, confs = extract_fields(tokens, h, w)
    return normalize_all(raw_values, confs)


def process_card(path: Path) -> Dict:
    if path.suffix.lower() == ".pdf":
        return {**process_card_bytes(path.read_bytes(), path.name), "file": str(path)}
    fields = _run_pipeline(path)
    return {
        "file": str(path),
        "fields": {k: asdict(v) for k, v in fields.items()},
        "needs_review": [k for k, v in fields.items() if not v.valid],
    }



def process_card_bytes(raw_bytes: bytes, label: str = "upload") -> Dict:
    """Same pipeline as process_card, supporting both image streams and PDF documents."""
    import io
    is_pdf = raw_bytes.startswith(b"%PDF") or label.lower().endswith(".pdf")
    if is_pdf:
        pages = pdf_to_images(raw_bytes)
        if not pages:
            # Fallback direct image open attempt if fitz is absent
            fields = _run_pipeline(io.BytesIO(raw_bytes))
        else:
            best_fields = None
            best_score = -1.0
            for p_bytes in pages:
                cur_fields = _run_pipeline(io.BytesIO(p_bytes))
                # Calculate composite confidence score
                pan_f = cur_fields.get("pan")
                name_f = cur_fields.get("name")
                dob_f = cur_fields.get("dob")
                score = (getattr(pan_f, "confidence", 0) or 0) * 2.0 + (getattr(name_f, "confidence", 0) or 0) + (getattr(dob_f, "confidence", 0) or 0)
                if getattr(pan_f, "valid", False):
                    score += 5.0
                if score > best_score:
                    best_score = score
                    best_fields = cur_fields
            fields = best_fields or _run_pipeline(io.BytesIO(pages[0]))
    else:
        fields = _run_pipeline(io.BytesIO(raw_bytes))

    field_dicts = {
        k: ({"value": v.value, "confidence": v.confidence, "valid": v.valid} if hasattr(v, "value") else v)
        for k, v in fields.items()
    }
    mapped = {
        "txtpanno": fields.get("pan").value if hasattr(fields.get("pan"), "value") else (field_dicts.get("pan", {}).get("value", "") or ""),
        "txtpname": fields.get("name").value if hasattr(fields.get("name"), "value") else (field_dicts.get("name", {}).get("value", "") or ""),
        "txtpdob": fields.get("dob").value if hasattr(fields.get("dob"), "value") else (field_dicts.get("dob", {}).get("value", "") or ""),
    }
    return {
        "file": label,
        "filename": label,
        "fields": field_dicts,
        "needs_review": [k for k, v in fields.items() if (hasattr(v, "valid") and not v.valid)],
        "mapped": mapped,
        "txtpanno": mapped["txtpanno"],
        "txtpname": mapped["txtpname"],
        "txtpdob": mapped["txtpdob"],
    }


def collect_images(inputs: List[str]) -> List[Path]:
    paths: List[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            paths.extend(sorted(f for f in p.iterdir() if f.suffix.lower() in IMAGE_EXTENSIONS))
        elif p.is_file():
            paths.append(p)
        else:
            print(f"Skipping '{item}': not found", file=sys.stderr)
    return paths


def print_report(result: Dict) -> None:
    print(f"\n{result['file']}")
    for field, info in result["fields"].items():
        flag = "OK " if info["valid"] else "?  "
        print(f"  {flag}{field:<8} {str(info['value']):<26} conf={info['confidence']:.2f}")
    if result["needs_review"]:
        print(f"  -> could not fully verify: {', '.join(result['needs_review'])}")


def main():
    parser = argparse.ArgumentParser(description="Extract PAN card fields from photos.")
    parser.add_argument("inputs", nargs="+", help="Image file(s) or folder(s) of images.")
    parser.add_argument("--json", metavar="FILE", help="Also write results to this JSON file.")
    args = parser.parse_args()

    images = collect_images(args.inputs)
    if not images:
        print("No images found.", file=sys.stderr)
        sys.exit(1)
    print(f"OCR engine: {OCR_ENGINE}"
          + ("" if OCR_ENGINE == "rapidocr" else
             "  (install rapidocr_onnxruntime for better accuracy: pip install rapidocr_onnxruntime)"))

    results = []
    for path in images:
        try:
            result = process_card(path)
        except Exception as exc:
            result = {"file": str(path), "error": str(exc)}
            print(f"\n{path}\n  ERROR: {exc}")
            results.append(result)
            continue
        print_report(result)
        results.append(result)

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
        print(f"\nWrote {len(results)} result(s) to {args.json}")


if __name__ == "__main__":
    main()
# Aliases for mail route compatibility
process_pan_bytes = process_card_bytes
process_pan_path = process_card


def mask_pan(val: str | None) -> str:
    if not val:
        return ""
    v = str(val).strip().upper()
    if len(v) == 10:
        return f"{v[:5]}****{v[-1]}"
    return v


def mask_name(val: str | None) -> str:
    if not val:
        return ""
    words = str(val).split()
    if len(words) > 1:
        masked_last = "*" * len(words[-1])
        return f"{words[0]} {masked_last}"
    masked_rest = "*" * max(0, len(str(val)) - 2)
    return f"{str(val)[:2]}{masked_rest}"
