#!/usr/bin/env python3
"""
aocr_core.py, Aadhaar card field extractor with name recognition.

Reads Aadhaar card photos AND PDFs (scanned or with text layer) and
extracts the 12-digit Aadhaar number plus the Aadhaar holder's name.
Uses a best-effort approach, always returns whatever it found, never
silently drops results.

Usage:
    python3 aocr_core.py card.jpg
    python3 aocr_core.py card1.jpg card2.png aadhaar_letter.pdf ./folder/
    python3 aocr_core.py ./cards/ --json results.json

Requirements (install once):
    pip install opencv-python-headless pytesseract pillow numpy pymupdf
    + the tesseract-ocr binary (apt install tesseract-ocr / brew install tesseract)
"""
import argparse
import io
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
    pytesseract = None  # type: ignore[assignment]
from PIL import Image, ImageOps

try:
    import pymupdf as fitz  # PyMuPDF, pip install pymupdf
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
PDF_EXTENSIONS   = {".pdf"}

# ===========================================================================
# 1. PDF → image conversion  (PyMuPDF: no external binaries required)
# ===========================================================================

def pdf_to_images(pdf_bytes: bytes) -> List[bytes]:
    """
    Convert each page of a PDF to JPEG bytes using PyMuPDF.
    Returns an empty list (with an stderr message) if the PDF can't be opened.
    """
    if fitz is None:
        print("[pymupdf] PyMuPDF not installed. Run: pip install pymupdf", file=sys.stderr)
        return []
    images: List[bytes] = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page in doc:
            pix = page.get_pixmap(dpi=250)
            images.append(pix.tobytes("jpeg"))
        doc.close()
    except Exception as exc:
        print(f"[pymupdf] failed to rasterize PDF: {exc}", file=sys.stderr)
    return images


def _extract_text_layer(pdf_bytes: bytes) -> str:
    """
    Try to extract a text layer from a PDF using PyMuPDF.
    Returns empty string if PyMuPDF is unavailable or PDF is image-only.
    """
    if fitz is None:
        print("[pymupdf] PyMuPDF not installed. Run: pip install pymupdf", file=sys.stderr)
        return ""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        return text
    except Exception as exc:
        print(f"[pymupdf] failed to extract text layer: {exc}", file=sys.stderr)
        return ""


# ===========================================================================
# 2. Loading + orientation
# ===========================================================================

HEADER_KEYWORDS = ("uidai", "unique identification", "aadhaar", "आधार", "government of india")
NAME_LABEL_PATTERNS = (r"^name$", r"^name:\s*$", r"^नाम$", r"^नाम:\s*$")


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
# 3. Preprocessing
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
# 4. OCR
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
    from core.pan_ocr import _rapid_engine, OCR_ENGINE  # share one RapidOCR instance
except ImportError:
    from pan_ocr import _rapid_engine, OCR_ENGINE  # script mode


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
                continue
            tokens.append(Token(
                text=text, conf=conf / 100.0,
                x=float(data["left"][i]), y=float(data["top"][i]),
                w=float(data["width"][i]), h=float(data["height"][i]),
            ))
    return tokens


# ===========================================================================
# 5. Field extraction
# ===========================================================================

AADHAAR_REGEX = re.compile(r"\b[2-9]\d{3}[\s\-]?\d{4}[\s\-]?\d{4}\b")

_FALLBACK_NAME_KEYWORDS = (
    r"name", r"नाम", r"aadhaar", r"uidai", r"government", r"income", r"tax"
)

_LETTER_TO_DIGIT = {
    "O": "0", "o": "0",
    "I": "1", "l": "1", "i": "1",
    "S": "5", "s": "5",
    "B": "8",
    "Z": "2", "z": "2",
    "G": "6",
    "T": "7",
    "A": "4",
}




def _aadhaar_from_text(text: str) -> Optional[str]:
    """
    Extract the real Aadhaar number from a plain-text string (e.g. PDF text layer).

    Skips numbers that are preceded within the same or adjacent line by a "VID"
    label, mirroring the VID-filtering logic used by find_aadhaar() on the image
    path.  Takes the first non-VID match so that a document with both a VID and a
    real Aadhaar number printed in the standard order (VID first) returns the
    correct number.
    """
    text = _normalize_digits(text)
    lines = text.splitlines()

    # Build a set of 12-digit strings that appear on lines tagged as VID lines.
    vid_numbers: set[str] = set()
    for line in lines:
        if re.search(r"\bvid\b", line, re.IGNORECASE):
            for m in AADHAAR_REGEX.finditer(line):
                vid_numbers.add(re.sub(r"[\s\-]", "", m.group(0)))

    # Return the first match that is not in the VID set.
    for m in AADHAAR_REGEX.finditer(text):
        candidate = re.sub(r"[\s\-]", "", m.group(0))
        if candidate not in vid_numbers:
            return candidate
    return None


def _is_name_label(text: str) -> bool:
    t = text.lower().strip()
    return any(re.search(p, t) for p in NAME_LABEL_PATTERNS)


def _is_skip_label(text: str) -> bool:
    t = text.lower().strip()
    return any(kw in t for kw in _FALLBACK_NAME_KEYWORDS if kw != "name")


def _is_name_like(text: str, strict: bool = True) -> bool:
    cleaned = text.strip()
    if len(cleaned) < 2:
        return False
    if re.search(r"\d", cleaned):
        return False
    alpha_ratio = sum(c.isalpha() or c in " .-'" for c in cleaned) / len(cleaned)
    min_alpha = 0.85 if strict else 0.65
    if alpha_ratio < min_alpha:
        return False
    letters = [c for c in cleaned if c.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(c.isupper() for c in letters) / len(letters)
    min_upper = 0.45 if strict else 0.30
    return upper_ratio >= min_upper


def _is_text_line_name_like(text: str) -> bool:
    cleaned = text.strip()
    if len(cleaned) < 3:
        return False
    if re.search(r"\d", cleaned):
        return False
    if _is_skip_label(cleaned):
        return False
    if any(re.search(p, cleaned.lower()) for p in NAME_LABEL_PATTERNS):
        return False
    alpha_ratio = sum(c.isalpha() or c in " .-'" for c in cleaned) / len(cleaned)
    if alpha_ratio < 0.70:
        return False
    letters = [c for c in cleaned if c.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(c.isupper() for c in letters) / len(letters)
    return upper_ratio >= 0.35 or cleaned[0].isupper()


def _name_from_text(text: str) -> Optional[str]:
    lines = [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]
    for i, line in enumerate(lines):
        normalized = line.lower()
        if any(re.search(p, normalized) for p in NAME_LABEL_PATTERNS):
            remainder = re.sub(r"^.*?(name[:\s]*|नाम[:\s]*)", "", line, flags=re.IGNORECASE).strip()
            if remainder:
                return _format_name(remainder)
            if i + 1 < len(lines) and _is_text_line_name_like(lines[i + 1]):
                return _format_name(lines[i + 1])
    # Fallback: take the first plausible name-like line that appears before the aadhaar number
    aadhaar_pos = None
    for i, line in enumerate(lines):
        if AADHAAR_REGEX.search(_normalize_digits(line)):
            aadhaar_pos = i
            break
    candidate_lines = []
    for i, line in enumerate(lines):
        if i == aadhaar_pos:
            continue
        if aadhaar_pos is not None and i > aadhaar_pos:
            break
        if _is_text_line_name_like(line):
            candidate_lines.append((i, line))
    if candidate_lines:
        return _format_name(candidate_lines[0][1])
    return None


def _deduplicate_tokens(tokens: List[Token]) -> List[Token]:
    if not tokens:
        return tokens
    kept: List[Token] = []
    for t in tokens:
        merged = False
        for k in kept:
            if (abs(t.x - k.x) < 5 and abs(t.y - k.y) < 5 and
                    abs(t.w - k.w) < 10 and abs(t.h - k.h) < 10):
                if t.conf > k.conf:
                    kept.remove(k)
                    kept.append(t)
                merged = True
                break
        if not merged:
            kept.append(t)
    return kept


def _best_candidate(cands):
    valid = [c for c in cands if _verhoeff_ok(c[0])]
    return max(valid or cands, key=lambda x: x[1])


def find_aadhaar(tokens: List[Token]) -> Tuple[Optional[str], float]:
    tokens = _deduplicate_tokens(tokens)
    candidates: List[Tuple[str, float]] = []

    for t in tokens:
        raw = _normalize_digits(t.text)
        m = AADHAAR_REGEX.search(raw)
        if m:
            number = re.sub(r"[\s\-]", "", m.group(0))
            candidates.append((number, t.conf))
            continue
        corrected = "".join(_LETTER_TO_DIGIT.get(ch, ch) for ch in raw)
        m = AADHAAR_REGEX.search(corrected)
        if m:
            number = re.sub(r"[\s\-]", "", m.group(0))
            candidates.append((number, t.conf))

    if candidates:
        return _best_candidate(candidates)

    joined = " ".join(_normalize_digits(t.text) for t in tokens)
    vid_fragments: set = set()
    for t in tokens:
        if re.search(r"\bvid\b", t.text, re.IGNORECASE):
            vid_fragments.update(re.findall(r"\d{4}", _normalize_digits(t.text + " " +
                " ".join(_normalize_digits(x.text) for x in tokens
                         if abs(x.cy - t.cy) < 30 and x.cx > t.cx))))
    s3_candidates: List[Tuple[str, float]] = []
    for m in AADHAAR_REGEX.finditer(joined):
        number = re.sub(r"[\s\-]", "", m.group(0))
        blocks = re.findall(r"\d{4}", number)
        if any(b in vid_fragments for b in blocks):
            continue
        if len(blocks) != len(set(blocks)):
            continue
        matching_confs = []
        for t in tokens:
            t_digits = re.sub(r"[^\d]", "", _normalize_digits(t.text))
            if len(t_digits) >= 4 and t_digits in number:
                matching_confs.append(t.conf)
        best_conf = sum(matching_confs) / len(matching_confs) if matching_confs else 0.5
        s3_candidates.append((number, best_conf))
    if any(_verhoeff_ok(c[0]) for c in s3_candidates):
        return _best_candidate(s3_candidates)

    vid_token_ids: set = set()
    for t in tokens:
        if re.search(r"\bvid\b", t.text, re.IGNORECASE):
            vid_token_ids.add(id(t))

    sorted_tokens = sorted(tokens, key=lambda t: (round(t.cy / 20), t.cx))
    vid_sorted_indices: set = set()
    for i, t in enumerate(sorted_tokens):
        if id(t) in vid_token_ids:
            for j in range(i + 1, min(len(sorted_tokens), i + 5)):
                if abs(sorted_tokens[j].cy - t.cy) < 30:
                    vid_sorted_indices.add(j)

    window_candidates: List[Tuple[str, float]] = []
    for window in range(2, 5):
        for i in range(len(sorted_tokens) - window + 1):
            if any(k in vid_sorted_indices for k in range(i, i + window)):
                continue
            chunk = " ".join(_normalize_digits(sorted_tokens[j].text) for j in range(i, i + window))
            m = AADHAAR_REGEX.search(chunk)
            if m:
                number = re.sub(r"[\s\-]", "", m.group(0))
                blocks = re.findall(r"\d{4}", number)
                if len(blocks) == len(set(blocks)):
                    avg_conf = sum(sorted_tokens[j].conf for j in range(i, i + window)) / window
                    window_candidates.append((number, avg_conf))
    if any(_verhoeff_ok(c[0]) for c in window_candidates):
        return _best_candidate(window_candidates)
    if s3_candidates or window_candidates:
        return _best_candidate(s3_candidates + window_candidates)

    # No last-resort scan over every digit glued together: ~1 in 10 random
    # 12-digit windows passes Verhoeff, so it invented UIDs (e.g. from a PAN card).
    return None, 0.0


def _format_name(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z\s.'\-]", "", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().upper()
    return cleaned


def _name_row_to_text(row: List[Token]) -> str:
    row = sorted(row, key=lambda t: t.cx)
    return " ".join(t.text for t in row).strip()


def _build_rows(tokens: List[Token]) -> List[List[Token]]:
    rows: List[List[Token]] = []
    for t in sorted(tokens, key=lambda x: x.cy):
        placed = False
        for row in rows:
            if abs(row[0].cy - t.cy) < 40:
                row.append(t)
                placed = True
                break
        if not placed:
            rows.append([t])
    return rows


def extract_fields(tokens: List[Token], img_h: float, img_w: float) -> Tuple[Dict, Dict]:
    values = {"aadhaar": None, "name": None}
    confs  = {"aadhaar": 0.0, "name": 0.0}

    aadhaar, aadhaar_conf = find_aadhaar(tokens)
    values["aadhaar"] = aadhaar
    confs["aadhaar"] = aadhaar_conf

    name_values: List[Tuple[str, float, float]] = []
    for t in tokens:
        if _is_name_label(t.text):
            name_values.append((t.text, t.conf, t.cy))

    if name_values:
        name_values.sort(key=lambda x: x[2])
        label_y = name_values[0][2]
        candidates = [t for t in tokens if t.cy > label_y and t.cy < label_y + img_h * 0.20 and _is_name_like(t.text, strict=False)]
        if candidates:
            rows = _build_rows(candidates)
            candidate = rows[0]
            values["name"] = _format_name(_name_row_to_text(candidate))
            confs["name"] = sum(t.conf for t in candidate) / len(candidate)

    if not values["name"]:
        name_like_tokens = [t for t in tokens if _is_name_like(t.text, strict=False)]
        if name_like_tokens:
            rows = _build_rows(name_like_tokens)
            for row in rows:
                text = _format_name(_name_row_to_text(row))
                if len(text) >= 3 and not _is_skip_label(text):
                    values["name"] = text
                    confs["name"] = sum(t.conf for t in row) / len(row)
                    break

    return values, confs


# ===========================================================================
# 6. Normalization
# ===========================================================================

AADHAAR_PATTERN = re.compile(r"^[2-9]\d{11}$")

_V_D = ((0,1,2,3,4,5,6,7,8,9),(1,2,3,4,0,6,7,8,9,5),(2,3,4,0,1,7,8,9,5,6),(3,4,0,1,2,8,9,5,6,7),(4,0,1,2,3,9,5,6,7,8),
        (5,9,8,7,6,0,4,3,2,1),(6,5,9,8,7,1,0,4,3,2),(7,6,5,9,8,2,1,0,4,3),(8,7,6,5,9,3,2,1,0,4),(9,8,7,6,5,4,3,2,1,0))
_V_P = ((0,1,2,3,4,5,6,7,8,9),(1,5,7,6,2,8,3,0,9,4),(5,8,0,3,7,9,6,1,4,2),(8,9,1,6,0,4,3,5,2,7),
        (9,4,5,3,1,2,6,8,7,0),(4,2,8,6,5,7,3,9,0,1),(2,7,9,3,8,0,6,4,1,5),(7,0,4,6,9,1,3,2,5,8))


def _verhoeff_ok(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _V_D[c][_V_P[i % 8][int(ch)]]
    return c == 0

@dataclass
class Field:
    value: Optional[str]
    confidence: float
    valid: bool


def _format_aadhaar(digits: str) -> str:
    return f"{digits[:4]} {digits[4:8]} {digits[8:]}"


def normalize_aadhaar(raw: Optional[str], conf: float) -> Field:
    if not raw:
        return Field(None, conf, False)
    digits = re.sub(r"[^\d]", "", raw)
    valid = bool(AADHAAR_PATTERN.match(digits)) and _verhoeff_ok(digits)
    display = _format_aadhaar(digits) if valid else digits
    return Field(display, conf, valid)


def normalize_name(raw: Optional[str], conf: float) -> Field:
    if not raw:
        return Field(None, conf, False)
    cleaned = _format_name(raw)
    valid = len(cleaned) >= 3 and any(c.isalpha() for c in cleaned)
    return Field(cleaned or None, conf, valid)


def normalize_all(values: Dict, confs: Dict) -> Dict[str, Field]:
    return {
        "aadhaar": normalize_aadhaar(values["aadhaar"], confs["aadhaar"]),
        "name":    normalize_name(values["name"],    confs["name"]),
    }


# ===========================================================================
# 7. Pipeline
# ===========================================================================

def _run_image_pipeline(source) -> Tuple[Optional[str], Optional[str], float, float]:
    image = load_and_orient(source)
    processed = preprocess(image)
    h, w = processed.shape[:2]
    tokens = run_ocr(processed)
    raw_values, confs = extract_fields(tokens, h, w)
    return raw_values["aadhaar"], raw_values["name"], confs["aadhaar"], confs["name"]


def _run_pipeline_bytes(raw_bytes: bytes) -> Dict[str, Field]:
    is_pdf = raw_bytes[:4] == b"%PDF"

    if is_pdf:
        text = _extract_text_layer(raw_bytes)
        if text:
            number = _aadhaar_from_text(text)
            # _name_from_text was previously hardcoded to None here, that was a
            # bug that silently discarded names from every text-layer PDF.
            name = _name_from_text(text)
            if number:
                # Only award high confidence to the Aadhaar number when we also
                # have a name; a number-only result from a text layer that could
                # contain a VID before the real number deserves a more cautious
                # score so it surfaces in needs_review when the name is missing.
                aadhaar_conf = 0.99 if name else 0.75
                return normalize_all(
                    {"aadhaar": number, "name": name},
                    {"aadhaar": aadhaar_conf, "name": 0.85 if name else 0.0},
                )

        page_bytes_list = pdf_to_images(raw_bytes)
        best_number, best_name, best_conf, best_name_conf = None, None, 0.0, 0.0
        for page_bytes in page_bytes_list:
            number, name, conf, name_conf = _run_image_pipeline(io.BytesIO(page_bytes))
            if number and conf > best_conf:
                best_number, best_conf = number, conf
            if name and name_conf > best_name_conf:
                best_name, best_name_conf = name, name_conf
        return normalize_all({"aadhaar": best_number, "name": best_name}, {"aadhaar": best_conf, "name": best_name_conf})

    number, name, conf, name_conf = _run_image_pipeline(io.BytesIO(raw_bytes))
    return normalize_all({"aadhaar": number, "name": name}, {"aadhaar": conf, "name": name_conf})


def process_card(path: Path) -> Dict:
    raw_bytes = path.read_bytes()
    fields = _run_pipeline_bytes(raw_bytes)
    return {
        "file": str(path),
        "fields": {k: asdict(v) for k, v in fields.items()},
        "needs_review": [k for k, v in fields.items() if not v.valid],
    }



def process_card_bytes(raw_bytes: bytes, label: str = "upload") -> Dict:
    fields = _run_pipeline_bytes(raw_bytes)
    field_dicts = {
        k: ({"value": v.value, "confidence": v.confidence, "valid": v.valid} if hasattr(v, "value") else v)
        for k, v in fields.items()
    }
    mapped = {
        "txtaadharno": fields.get("aadhaar").value if hasattr(fields.get("aadhaar"), "value") else (field_dicts.get("aadhaar", {}).get("value", "") or ""),
        "txtaname": fields.get("name").value if hasattr(fields.get("name"), "value") else (field_dicts.get("name", {}).get("value", "") or ""),
    }
    return {
        "file": label,
        "filename": label,
        "fields": field_dicts,
        "raw_fields": {k: asdict(v) for k, v in fields.items()},
        "needs_review": [k for k, v in fields.items() if (hasattr(v, "valid") and not v.valid)],
        "mapped": mapped,
        "txtaadharno": mapped["txtaadharno"],
        "txtaname": mapped["txtaname"],
    }


# ===========================================================================
# 8. CLI
# ===========================================================================

def collect_images(inputs: List[str]) -> List[Path]:
    all_exts = IMAGE_EXTENSIONS | PDF_EXTENSIONS
    paths: List[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            paths.extend(sorted(f for f in p.iterdir() if f.suffix.lower() in all_exts))
        elif p.is_file():
            paths.append(p)
        else:
            print(f"Skipping '{item}': not found", file=sys.stderr)
    return paths


def print_report(result: Dict) -> None:
    print(f"\n{result['file']}")
    for field, info in result["fields"].items():
        flag = "OK " if info["valid"] else "?  "
        print(f"  {flag}{field:<10} {str(info['value']):<20} conf={info['confidence']:.2f}")
    if result["needs_review"]:
        print(f"  -> could not fully verify: {', '.join(result['needs_review'])}")


def main():
    parser = argparse.ArgumentParser(description="Extract Aadhaar number and name from card photos or PDFs.")
    parser.add_argument("inputs", nargs="+", help="Image file(s), PDF(s), or folder(s).")
    parser.add_argument("--json", metavar="FILE", help="Also write results to this JSON file.")
    args = parser.parse_args()

    images = collect_images(args.inputs)
    if not images:
        print("No images or PDFs found.", file=sys.stderr)
        sys.exit(1)
    print(f"OCR engine: {OCR_ENGINE}"
          + ("" if OCR_ENGINE == "rapidocr" else
             "  (install rapidocr_onnxruntime for better accuracy: pip install rapidocr_onnxruntime)"))
    if fitz is None:
        print("WARNING: PyMuPDF not installed, PDF support disabled. Run: pip install pymupdf", file=sys.stderr)

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


def mask_aadhaar(val: str | None) -> str:
    if not val:
        return ""
    digits = re.sub(r"\D", "", str(val))
    if len(digits) == 12:
        return f"XXXX XXXX {digits[-4:]}"
    return str(val)


def mask_name(val: str | None) -> str:
    if not val:
        return ""
    words = str(val).split()
    if len(words) > 1:
        masked_last = "*" * len(words[-1])
        return f"{words[0]} {masked_last}"
    masked_rest = "*" * max(0, len(str(val)) - 2)
    return f"{str(val)[:2]}{masked_rest}"
