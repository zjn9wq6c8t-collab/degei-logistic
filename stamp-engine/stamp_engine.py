from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import pdfplumber
from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

try:
    from pdf2image import convert_from_path
except Exception:  # pragma: no cover - production should install pdf2image/poppler.
    convert_from_path = None


TARGET_PHRASES = [
    ("CARRIER CONFIRMATION", 145),
    ("HAULIER CONFIRMATION", 145),
    ("STAMPILA SI SEMNATURA TRANSPORTATORULUI", 170),
    ("SEMNATURA SI STAMPILA TRANSPORTATORULUI", 170),
    ("STAMPILA SI SEMNATURA TRANSPORTATOR", 165),
    ("SEMNATURA SI STAMPILA TRANSPORTATOR", 165),
    ("SEMNATURA TRANSPORTATORULUI", 155),
    ("TRANSPORTATOR CONFIRMARE", 130),
    ("CONFIRMARE TRANSPORTATOR", 120),
    ("CONFIRMAREA TRANSPORTATORULUI", 120),
    ("SEMNATURA SI STAMPILA", 115),
    ("SEMNATURA SI SEMNATURA", 100),
    ("SEMNATURA TRANSPORTATOR", 115),
    ("STAMPILA TRANSPORTATORULUI", 155),
    ("STAMPILA TRANSPORTATOR", 115),
    ("TRANSPORTATOR", 90),
    ("CARAUS", 100),
    ("CARRIER", 90),
    ("HAULIER", 90),
    ("VETTORE", 90),
    ("FRACHTFUHRER", 90),
    ("TRANSPORTEUR", 90),
    ("TRANSPORTISTA", 90),
    ("SIGNATURE AND STAMP", 110),
    ("SIGN AND STAMP", 100),
    ("CARRIER SIGNATURE AND STAMP", 160),
    ("HAULIER SIGNATURE AND STAMP", 160),
    ("TIMBRO E FIRMA DEL VETTORE", 160),
    ("FIRMA E TIMBRO DEL VETTORE", 160),
    ("TIMBRO E FIRMA", 110),
    ("UNTERSCHRIFT UND STEMPEL DES FRACHTFUHRERS", 160),
    ("UNTERSCHRIFT UND STEMPEL", 110),
    ("SIGNATURE ET CACHET DU TRANSPORTEUR", 160),
    ("SIGNATURE ET CACHET", 110),
    ("FIRMA Y SELLO DEL TRANSPORTISTA", 160),
    ("FIRMA Y SELLO", 110),
    ("DEGEI LOGISTIC", 75),
    ("RO36256981", 70),
    ("FURNIZOR", 65),
    ("PRESTATOR", 65),
    ("SUBCONTRACTANT", 65),
]

GENERIC_TARGETS = {
    "TRANSPORTATOR",
    "CARAUS",
    "CARRIER",
    "HAULIER",
    "VETTORE",
    "FRACHTFUHRER",
    "TRANSPORTEUR",
    "TRANSPORTISTA",
}
FOOTER_TARGETS = {"DEGEI LOGISTIC", "RO36256981"}
TRANSPORTER_NAME_TARGETS = {"DEGEI LOGISTIC", "RO36256981"}
SUPPLIER_SIGNATURE_TARGETS = {"FURNIZOR", "PRESTATOR", "SUBCONTRACTANT"}
SIGNATURE_LABEL_TARGETS = {
    "STAMPILA SI SEMNATURA TRANSPORTATORULUI",
    "SEMNATURA SI STAMPILA TRANSPORTATORULUI",
    "STAMPILA SI SEMNATURA TRANSPORTATOR",
    "SEMNATURA SI STAMPILA TRANSPORTATOR",
    "SEMNATURA TRANSPORTATORULUI",
    "SEMNATURA SI STAMPILA",
    "SEMNATURA SI SEMNATURA",
    "SEMNATURA TRANSPORTATOR",
    "STAMPILA TRANSPORTATORULUI",
    "STAMPILA TRANSPORTATOR",
    "SIGNATURE AND STAMP",
    "SIGN AND STAMP",
    "CARRIER SIGNATURE AND STAMP",
    "HAULIER SIGNATURE AND STAMP",
    "TIMBRO E FIRMA DEL VETTORE",
    "FIRMA E TIMBRO DEL VETTORE",
    "TIMBRO E FIRMA",
    "UNTERSCHRIFT UND STEMPEL DES FRACHTFUHRERS",
    "UNTERSCHRIFT UND STEMPEL",
    "SIGNATURE ET CACHET DU TRANSPORTEUR",
    "SIGNATURE ET CACHET",
    "FIRMA Y SELLO DEL TRANSPORTISTA",
    "FIRMA Y SELLO",
}
OVERLAP_OK_SIGNATURE_TARGETS = {
    "STAMPILA SI SEMNATURA TRANSPORTATORULUI",
    "SEMNATURA SI STAMPILA TRANSPORTATORULUI",
    "STAMPILA SI SEMNATURA TRANSPORTATOR",
    "SEMNATURA SI STAMPILA TRANSPORTATOR",
    "SEMNATURA TRANSPORTATORULUI",
    "SEMNATURA TRANSPORTATOR",
    "STAMPILA TRANSPORTATORULUI",
    "STAMPILA TRANSPORTATOR",
    "CARRIER SIGNATURE AND STAMP",
    "HAULIER SIGNATURE AND STAMP",
    "TIMBRO E FIRMA DEL VETTORE",
    "FIRMA E TIMBRO DEL VETTORE",
    "UNTERSCHRIFT UND STEMPEL DES FRACHTFUHRERS",
    "SIGNATURE ET CACHET DU TRANSPORTEUR",
    "FIRMA Y SELLO DEL TRANSPORTISTA",
}
NON_CARRIER_SIGNATURE_MARKERS = {
    "INCARCATOR",
    "DESTINATAR",
    "DESTINARAR",
    "EXPEDITOR",
    "BENEFICIAR",
    "CLIENT",
    "PRIMITOR",
    "CONSIGNOR",
    "CONSIGNEE",
    "SHIPPER",
    "RECEIVER",
    "MITTENTE",
    "DESTINATARIO",
}
CARRIER_SIGNATURE_MARKERS = {
    "TRANSPORTATOR",
    "CARAUS",
    "CARRIER",
    "HAULIER",
    "VETTORE",
    "FRACHTFUHRER",
    "TRANSPORTEUR",
    "TRANSPORTISTA",
    "PRESTATOR",
    "FURNIZOR",
    "SUBCONTRACTANT",
    "DEGEI",
}
CONFIRMATION_HEADING_TARGETS = {
    "CARRIER CONFIRMATION",
    "HAULIER CONFIRMATION",
    "TRANSPORTATOR CONFIRMARE",
    "CONFIRMARE TRANSPORTATOR",
    "CONFIRMAREA TRANSPORTATORULUI",
}

OCR_LANGUAGES = os.environ.get("STAMP_OCR_LANGS", "ron+eng+deu+ita+fra+spa")
OCR_DPI = min(max(int(os.environ.get("STAMP_OCR_DPI", "200")), 150), 300)
OCR_NATIVE_WORD_THRESHOLD = min(
    max(int(os.environ.get("STAMP_OCR_NATIVE_WORD_THRESHOLD", "14")), 0),
    100,
)
OCR_CANONICAL_TOKENS = {
    "TRANSPORTATOR",
    "TRANSPORTATORULUI",
    "CARAUS",
    "SEMNATURA",
    "STAMPILA",
    "CONFIRMARE",
    "CONFIRMAREA",
    "DEGEI",
    "LOGISTIC",
    "CARRIER",
    "HAULIER",
    "SIGNATURE",
    "STAMP",
    "VETTORE",
    "FRACHTFUHRER",
    "TRANSPORTEUR",
    "TRANSPORTISTA",
    "TIMBRO",
    "FIRMA",
    "FRACHTFUHRERS",
    "UNTERSCHRIFT",
    "STEMPEL",
    "TRANSPORTEUR",
    "CACHET",
    "TRANSPORTISTA",
    "SELLO",
}
DEDICATED_TARGETS = {
    phrase
    for phrase, _score in TARGET_PHRASES
    if phrase not in FOOTER_TARGETS
    and phrase not in GENERIC_TARGETS
    and phrase not in TRANSPORTER_NAME_TARGETS
    and phrase not in SUPPLIER_SIGNATURE_TARGETS
}


@dataclass(frozen=True)
class Box:
    x0: float
    top: float
    x1: float
    bottom: float

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.bottom - self.top)

    @property
    def area(self) -> float:
        return self.width * self.height


@dataclass(frozen=True)
class Anchor:
    page_index: int
    box: Box
    score: int
    phrase: str
    line_text: str


@dataclass(frozen=True)
class Placement:
    page_index: int
    rect: Box
    score: float
    anchor_phrase: str
    reason: str


def norm(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def contains_phrase(line_norm: str, phrase: str) -> bool:
    return re.search(rf"(^| ){re.escape(phrase)}($| )", line_norm) is not None


def canonicalize_ocr_word(text: str) -> str:
    """Repair small OCR errors only for words that drive stamp placement."""
    token = norm(text)
    if len(token) < 4 or any(ch.isdigit() for ch in token):
        return text

    best = max(
        OCR_CANONICAL_TOKENS,
        key=lambda candidate: SequenceMatcher(None, token, candidate).ratio(),
    )
    similarity = SequenceMatcher(None, token, best).ratio()
    threshold = 0.86 if len(token) >= 8 else 0.90
    return best if similarity >= threshold else text


def tesseract_command() -> str | None:
    configured = os.environ.get("TESSERACT_CMD", "").strip()
    if configured:
        return configured
    return shutil.which("tesseract")


def ocr_page_words(
    pdf_path: Path,
    page_index: int,
    page_w: float,
    page_h: float,
    crop_top_ratio: float = 0.0,
) -> list[dict]:
    """Return OCR words in PDF top-left coordinates for an image-only page."""
    command = tesseract_command()
    if command is None or convert_from_path is None:
        return []

    try:
        images = convert_from_path(
            str(pdf_path),
            dpi=OCR_DPI,
            first_page=page_index + 1,
            last_page=page_index + 1,
            fmt="png",
            thread_count=1,
        )
    except Exception:
        return []
    if not images:
        return []

    full_image = ImageOps.autocontrast(images[0].convert("L"))
    full_width, full_height = full_image.size
    crop_y = int(full_height * min(max(crop_top_ratio, 0.0), 0.80))
    image = full_image.crop((0, crop_y, full_width, full_height))
    scale_x = page_w / max(float(full_width), 1.0)
    scale_y = page_h / max(float(full_height), 1.0)

    with tempfile.TemporaryDirectory(prefix="degei_ocr_") as temp_dir:
        image_path = Path(temp_dir) / "page.png"
        image.save(image_path, format="PNG", optimize=True)
        args = [
            command,
            str(image_path),
            "stdout",
            "-l",
            OCR_LANGUAGES,
            "--psm",
            "11",
            "-c",
            "tessedit_create_tsv=1",
        ]
        try:
            completed = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=75,
            )
        except (OSError, subprocess.SubprocessError):
            return []

    if completed.returncode != 0 or not completed.stdout.strip():
        return []

    words = []
    for row in csv.DictReader(io.StringIO(completed.stdout), delimiter="\t"):
        raw_text = str(row.get("text", "")).strip()
        if not norm(raw_text):
            continue
        try:
            confidence = float(row.get("conf", "-1"))
            left = float(row.get("left", "0"))
            top = float(row.get("top", "0"))
            width = float(row.get("width", "0"))
            height = float(row.get("height", "0"))
        except (TypeError, ValueError):
            continue
        if confidence < 22 or width <= 0 or height <= 0:
            continue

        words.append(
            {
                "text": canonicalize_ocr_word(raw_text),
                "x0": left * scale_x,
                "top": (top + crop_y) * scale_y,
                "x1": (left + width) * scale_x,
                "bottom": (top + crop_y + height) * scale_y,
            }
        )
    return words


def looks_like_carrier_signature_label(
    line_norm: str,
    phrase: str,
    box: Box,
    page_h: float,
) -> bool:
    """Reject legal prose and signature fields that belong to another party."""
    if phrase not in SIGNATURE_LABEL_TARGETS:
        return True
    has_non_carrier_marker = any(
        marker in line_norm for marker in NON_CARRIER_SIGNATURE_MARKERS
    )
    has_carrier_marker = any(marker in line_norm for marker in CARRIER_SIGNATURE_MARKERS)
    if has_non_carrier_marker and not has_carrier_marker:
        return False
    if phrase in OVERLAP_OK_SIGNATURE_TARGETS:
        return True
    if line_norm == phrase:
        return True
    phrase_tokens = phrase.split()
    line_tokens = line_norm.split()
    if (
        phrase_tokens
        and len(line_tokens) >= len(phrase_tokens) * 2
        and len(line_tokens) % len(phrase_tokens) == 0
        and all(
            line_tokens[index : index + len(phrase_tokens)] == phrase_tokens
            for index in range(0, len(line_tokens), len(phrase_tokens))
        )
    ):
        # Two-column forms often repeat the same short signature heading on
        # one visual line. Keep both cells eligible so the carrier-side one
        # can be selected later.
        return True
    if any(marker in line_norm for marker in CARRIER_SIGNATURE_MARKERS):
        return len(line_norm.split()) <= 12

    # A short generic label can be valid in the signature half of a form. A
    # longer or higher line is usually a contractual sentence mentioning that
    # somebody must sign/stamp a CMR, not a place where we should stamp.
    return len(line_norm.split()) <= 4 and box.top >= page_h * 0.45


def group_lines(words: list[dict]) -> list[dict]:
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (round(float(w["top"]) / 4), float(w["x0"]))):
        placed = False
        mid = (float(word["top"]) + float(word["bottom"])) / 2
        for line in lines:
            lmids = [(float(w["top"]) + float(w["bottom"])) / 2 for w in line]
            if abs(mid - (sum(lmids) / len(lmids))) <= 5:
                line.append(word)
                placed = True
                break
        if not placed:
            lines.append([word])

    packed = []
    for line_words in lines:
        line_words = sorted(line_words, key=lambda w: float(w["x0"]))
        text = " ".join(str(w.get("text", "")) for w in line_words)
        packed.append(
            {
                "text": text,
                "norm": norm(text),
                "words": line_words,
                "box": Box(
                    min(float(w["x0"]) for w in line_words),
                    min(float(w["top"]) for w in line_words),
                    max(float(w["x1"]) for w in line_words),
                    max(float(w["bottom"]) for w in line_words),
                ),
            }
        )
    return packed


def phrase_boxes(line_words: list[dict], phrase: str) -> list[Box]:
    norm_words = [norm(str(w.get("text", ""))) for w in line_words]
    phrase_words = phrase.split()
    matches: list[Box] = []
    for start in range(0, len(norm_words) - len(phrase_words) + 1):
        if norm_words[start : start + len(phrase_words)] == phrase_words:
            matched = line_words[start : start + len(phrase_words)]
            matches.append(
                Box(
                    min(float(w["x0"]) for w in matched),
                    min(float(w["top"]) for w in matched),
                    max(float(w["x1"]) for w in matched),
                    max(float(w["bottom"]) for w in matched),
                )
            )
    return matches


def phrase_box(line_words: list[dict], phrase: str) -> Box | None:
    matches = phrase_boxes(line_words, phrase)
    return matches[0] if matches else None


def role_line_is_signature_context(line_norm: str, phrase: str) -> bool:
    tokens = line_norm.split()
    phrase_tokens = set(phrase.split())
    context_tokens = [token for token in tokens if token not in phrase_tokens]
    allowed_context = {
        "COMPANIE",
        "EXPEDITIE",
        "INTOCMIT",
        "DE",
        "CONFIRMARE",
        "CONFIRMAREA",
        "SEMNATURA",
        "STAMPILA",
    }
    return not context_tokens or all(token in allowed_context for token in context_tokens)


def words_have_signature_signal(words: list[dict], page_w: float, page_h: float) -> bool:
    """Cheap pre-check used to avoid OCR when native PDF text is sufficient."""
    for line in group_lines(words):
        line_norm = line["norm"]
        line_box = line["box"]
        if any(contains_phrase(line_norm, phrase) for phrase in SIGNATURE_LABEL_TARGETS):
            if len(line_norm.split()) <= 16 and line_box.top >= page_h * 0.35:
                return True
        if any(contains_phrase(line_norm, phrase) for phrase in GENERIC_TARGETS):
            for phrase in GENERIC_TARGETS:
                if (
                    contains_phrase(line_norm, phrase)
                    and role_line_is_signature_context(line_norm, phrase)
                    and line_box.top >= page_h * 0.50
                ):
                    return True
        if (
            (contains_phrase(line_norm, "DEGEI LOGISTIC") or contains_phrase(line_norm, "RO36256981"))
            and line_box.top >= page_h * 0.45
        ):
            return True
    return False


def find_anchors(pdf_path: Path) -> tuple[list[Anchor], list[list[Box]], list[list[Box]], list[tuple[float, float]]]:
    anchors: list[Anchor] = []
    all_word_boxes: list[list[Box]] = []
    all_image_boxes: list[list[Box]] = []
    page_sizes: list[tuple[float, float]] = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_index, page in enumerate(pdf.pages):
            page_sizes.append((float(page.width), float(page.height)))
            native_words = page.extract_words(
                x_tolerance=2,
                y_tolerance=3,
                keep_blank_chars=False,
                use_text_flow=False,
            )
            page_w = float(page.width)
            page_h = float(page.height)
            words = native_words
            if len(native_words) <= OCR_NATIVE_WORD_THRESHOLD:
                ocr_words = ocr_page_words(
                    pdf_path,
                    page_index,
                    page_w,
                    page_h,
                )
                if len(ocr_words) > len(native_words):
                    words = ocr_words
            elif not words_have_signature_signal(native_words, page_w, page_h):
                signature_band_words = ocr_page_words(
                    pdf_path,
                    page_index,
                    page_w,
                    page_h,
                    crop_top_ratio=0.38,
                )
                if words_have_signature_signal(signature_band_words, page_w, page_h):
                    words = [
                        word
                        for word in native_words
                        if float(word["top"]) < page_h * 0.38
                    ] + signature_band_words
            boxes = [
                Box(float(w["x0"]), float(w["top"]), float(w["x1"]), float(w["bottom"]))
                for w in words
            ]
            all_word_boxes.append(boxes)
            all_image_boxes.append(
                [
                    Box(float(img["x0"]), float(img["top"]), float(img["x1"]), float(img["bottom"]))
                    for img in page.images
                    if float(img.get("width", 0)) >= 30 and float(img.get("height", 0)) >= 30
                ]
            )

            lines = group_lines(words)
            for line in lines:
                line_norm = line["norm"]
                for phrase, base_score in TARGET_PHRASES:
                    if contains_phrase(line_norm, phrase):
                        matched_boxes = phrase_boxes(line["words"], phrase)
                        if not matched_boxes:
                            matched_boxes = [line["box"]]
                        if phrase in SIGNATURE_LABEL_TARGETS and (
                            len(matched_boxes) == 1
                            and (len(line_norm) > 55 or line["box"].width > page.width * 0.55)
                        ):
                            continue
                        if phrase in SIGNATURE_LABEL_TARGETS and not looks_like_carrier_signature_label(
                            line_norm,
                            phrase,
                            line["box"],
                            float(page.height),
                        ):
                            continue
                        # Generic words appear often in legal/payment paragraphs. They are valid
                        # anchors only when they look like a short field label, not body text.
                        if phrase in GENERIC_TARGETS:
                            if len(line_norm) > 38:
                                continue
                            split_column_role = (
                                len(line_norm.split()) <= 8
                                and all(box.width <= page.width * 0.22 for box in matched_boxes)
                                and all(
                                    box.x1 <= page.width * 0.56 or box.x0 >= page.width * 0.44
                                    for box in matched_boxes
                                )
                            )
                            if line["box"].width > page.width * 0.42 and not split_column_role:
                                continue
                        for anchor_box in matched_boxes:
                            score = base_score
                            if page_index == len(pdf.pages) - 1:
                                score += 15
                            if anchor_box.top > page.height * 0.45:
                                score += 8
                            if phrase in FOOTER_TARGETS and looks_like_carrier_footer(
                                anchor_box,
                                float(page.width),
                                float(page.height),
                            ):
                                score += 65
                            if phrase in TRANSPORTER_NAME_TARGETS and looks_like_carrier_signature_block(
                                anchor_box,
                                float(page.width),
                                float(page.height),
                            ):
                                score += 95
                            if phrase in SUPPLIER_SIGNATURE_TARGETS and looks_like_supplier_signature_heading(
                                anchor_box,
                                float(page.width),
                                float(page.height),
                            ):
                                score += 180
                            anchors.append(
                                Anchor(
                                    page_index=page_index,
                                    box=anchor_box,
                                    score=score,
                                    phrase=phrase,
                                    line_text=line["text"],
                                )
                            )
    return anchors, all_word_boxes, all_image_boxes, page_sizes


def looks_like_carrier_footer(box: Box, page_w: float, page_h: float) -> bool:
    return box.top > page_h * 0.78 and box.x0 > page_w * 0.45


def looks_like_carrier_signature_block(box: Box, page_w: float, page_h: float) -> bool:
    # Printed order footers can sit below 90% of the page height. They still
    # represent a valid carrier signature block when they are in a side column.
    in_lower_page = page_h * 0.52 < box.top < page_h * 0.975
    in_side_column = box.x0 < page_w * 0.42 or box.x1 > page_w * 0.58
    return in_lower_page and in_side_column


def looks_like_supplier_signature_heading(box: Box, page_w: float, page_h: float) -> bool:
    return box.top > page_h * 0.55 and box.x0 > page_w * 0.50


def is_split_transporter_name_pair(
    anchor: Anchor,
    anchors: list[Anchor],
    page_w: float,
    page_h: float,
) -> bool:
    """Recognize a carrier role and DEGEI name split over neighboring lines."""
    if anchor.phrase not in TRANSPORTER_NAME_TARGETS:
        return False

    anchor_center = (anchor.box.x0 + anchor.box.x1) / 2
    anchor_on_right = anchor_center >= page_w * 0.50
    line_norm = norm(anchor.line_text)
    same_line_pair = "TRANSPORTATOR" in line_norm and (
        "DEGEI LOGISTIC" in line_norm or "RO36256981" in line_norm
    )

    for label in anchors:
        if label.page_index != anchor.page_index:
            continue
        if (
            label.phrase not in GENERIC_TARGETS
            and label.phrase not in CONFIRMATION_HEADING_TARGETS
        ):
            continue
        label_center = (label.box.x0 + label.box.x1) / 2
        same_side = (label_center >= page_w * 0.50) == anchor_on_right
        vertical_gap = min(
            abs(label.box.bottom - anchor.box.top),
            abs(anchor.box.bottom - label.box.top),
        )
        horizontally_related = (
            label.box.x1 >= anchor.box.x0 - page_w * 0.18
            and label.box.x0 <= anchor.box.x1 + page_w * 0.18
        )
        if (
            same_side
            and horizontally_related
            and vertical_gap <= max(52.0, page_h * 0.085)
            and min(label.box.top, anchor.box.top) >= page_h * 0.38
        ):
            return True

    return (
        same_line_pair
        and page_h * 0.08 <= anchor.box.top <= page_h * 0.95
        and (anchor.page_index > 0 or anchor.box.top >= page_h * 0.52)
        and (anchor.box.x0 >= page_w * 0.48 or anchor.box.x1 <= page_w * 0.52)
    )


def is_verified_caraus_company_pair(
    anchor: Anchor,
    anchors: list[Anchor],
    page_w: float,
    page_h: float,
) -> bool:
    """Allow a higher-page Caraus field only when DEGEI is directly beside it."""
    if anchor.phrase not in TRANSPORTER_NAME_TARGETS:
        return False
    if not (page_h * 0.16 <= anchor.box.top <= page_h * 0.92):
        return False

    anchor_center = (anchor.box.x0 + anchor.box.x1) / 2
    anchor_on_right = anchor_center >= page_w * 0.50
    for role in anchors:
        if role.page_index != anchor.page_index or role.phrase != "CARAUS":
            continue
        role_center = (role.box.x0 + role.box.x1) / 2
        same_side = (role_center >= page_w * 0.50) == anchor_on_right
        vertical_gap = anchor.box.top - role.box.bottom
        horizontally_related = (
            role.box.x1 >= anchor.box.x0 - page_w * 0.10
            and role.box.x0 <= anchor.box.x1 + page_w * 0.10
            and abs(role_center - anchor_center) <= page_w * 0.16
        )
        if (
            same_side
            and horizontally_related
            and len(norm(role.line_text).split()) <= 4
            and -6.0 <= vertical_gap <= max(58.0, page_h * 0.085)
            and role.box.top >= page_h * 0.16
        ):
            return True
    return False


def is_carrier_identity_header(
    anchor: Anchor,
    anchors: list[Anchor],
    page_w: float,
    page_h: float,
) -> bool:
    """Reject DEGEI identity rows near the order header."""
    if anchor.phrase not in TRANSPORTER_NAME_TARGETS:
        return False

    line_norm = norm(anchor.line_text)
    if "TRANSPORTATOR" in line_norm and any(
        marker in line_norm
        for marker in ("NUME FIRMA", "DENUMIRE FIRMA", "DATE FIRMA", "DATE TRANSPORTATOR")
    ):
        return True
    if anchor.box.top >= page_h * 0.62:
        return False

    for tax_anchor in anchors:
        if tax_anchor.page_index != anchor.page_index or tax_anchor.phrase != "RO36256981":
            continue
        vertical_gap = tax_anchor.box.top - anchor.box.bottom
        horizontally_related = (
            tax_anchor.box.x1 >= anchor.box.x0 - page_w * 0.12
            and tax_anchor.box.x0 <= anchor.box.x1 + page_w * 0.12
        )
        if 0 <= vertical_gap <= max(34.0, page_h * 0.055) and horizontally_related:
            return True
    return False


def is_standalone_carrier_role_anchor(
    anchor: Anchor,
    word_boxes: list[Box],
    page_w: float,
    page_h: float,
) -> bool:
    """Recognize an isolated carrier heading followed by a writable area."""
    if anchor.phrase not in GENERIC_TARGETS or anchor.phrase == "CARAUS":
        return False
    line_norm = norm(anchor.line_text)
    if any(marker in line_norm for marker in NON_CARRIER_SIGNATURE_MARKERS):
        return False
    if (
        not role_line_is_signature_context(line_norm, anchor.phrase)
        or len(line_norm.split()) > 6
        or anchor.box.top < page_h * 0.55
    ):
        return False

    on_right = (anchor.box.x0 + anchor.box.x1) / 2 >= page_w * 0.50
    if on_right and anchor.box.x0 < page_w * 0.42:
        return False
    if not on_right and anchor.box.x1 > page_w * 0.58:
        return False

    same_column_below = [
        box
        for box in word_boxes
        if box.top >= anchor.box.bottom + 2.0
        and ((box.x0 + box.x1) / 2 >= page_w * 0.50) == on_right
    ]
    next_text_top = min(
        (box.top for box in same_column_below),
        default=page_h - 14.0,
    )
    return next_text_top - anchor.box.bottom >= max(38.0, page_h * 0.052)


def is_footer_anchor(anchor: Anchor, page_w: float, page_h: float) -> bool:
    return anchor.phrase in FOOTER_TARGETS and looks_like_carrier_footer(anchor.box, page_w, page_h)


def is_signature_block_anchor(anchor: Anchor, page_w: float, page_h: float) -> bool:
    if anchor.phrase in TRANSPORTER_NAME_TARGETS:
        return looks_like_carrier_signature_block(anchor.box, page_w, page_h)
    if anchor.phrase in SUPPLIER_SIGNATURE_TARGETS:
        return looks_like_supplier_signature_heading(anchor.box, page_w, page_h)
    return False


def anchor_rank(anchor: Anchor, page_w: float, page_h: float) -> tuple[int, int, float]:
    if anchor.phrase in SUPPLIER_SIGNATURE_TARGETS and looks_like_supplier_signature_heading(anchor.box, page_w, page_h):
        return (4, anchor.score + 180, anchor.box.top)
    if anchor.phrase in DEDICATED_TARGETS:
        return (3, anchor.score, anchor.box.top)
    if is_signature_block_anchor(anchor, page_w, page_h):
        return (3, anchor.score, anchor.box.top)
    if is_footer_anchor(anchor, page_w, page_h):
        return (2, anchor.score, anchor.box.top)
    return (1, anchor.score, anchor.box.top)


def overlap_area(a: Box, b: Box) -> float:
    x0 = max(a.x0, b.x0)
    y0 = max(a.top, b.top)
    x1 = min(a.x1, b.x1)
    y1 = min(a.bottom, b.bottom)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def overlap_ratio(rect: Box, word_boxes: list[Box]) -> float:
    overlap = sum(overlap_area(rect, w) for w in word_boxes)
    return overlap / max(rect.area, 1.0)


def clamp_rect(rect: Box, page_w: float, page_h: float, margin: float = 18) -> Box:
    width = rect.width
    height = rect.height
    x0 = min(max(rect.x0, margin), max(margin, page_w - width - margin))
    top = min(max(rect.top, margin), max(margin, page_h - height - margin))
    return Box(x0, top, x0 + width, top + height)


def reference_stamp_box(anchor: Anchor, image_boxes: list[Box]) -> Box | None:
    if anchor.phrase in SUPPLIER_SIGNATURE_TARGETS:
        candidates = [
            box
            for box in image_boxes
            if 45 <= box.width <= 170
            and 45 <= box.height <= 190
            and box.x1 < anchor.box.x0 - 35
            and abs(((box.top + box.bottom) / 2) - (anchor.box.bottom + 55)) < 180
        ]
        if candidates:
            return max(candidates, key=lambda b: b.area)
    candidates = []
    for box in image_boxes:
        ratio = box.width / max(box.height, 1.0)
        if not (
            40 <= box.width <= 190
            and 40 <= box.height <= 190
            and 0.35 <= ratio <= 2.80
        ):
            continue

        # Client stamps are normally in the opposite signature column. Support
        # both layouts: client on the left / carrier on the right and vice versa.
        opposite_column = box.x1 < anchor.box.x0 - 8 or box.x0 > anchor.box.x1 + 8
        vertical_distance = abs(
            ((box.top + box.bottom) / 2)
            - (anchor.box.bottom + 35)
        )
        if opposite_column and vertical_distance < 170:
            candidates.append(box)
    if not candidates:
        return None

    def reference_rank(box: Box) -> tuple[float, float, float]:
        if box.x1 < anchor.box.x0:
            horizontal_gap = anchor.box.x0 - box.x1
        else:
            horizontal_gap = box.x0 - anchor.box.x1
        vertical_distance = abs(
            ((box.top + box.bottom) / 2)
            - (anchor.box.bottom + 35)
        )
        return (-vertical_distance, -horizontal_gap, box.area)

    return max(candidates, key=reference_rank)


def is_paired_signature_anchor(
    anchor: Anchor,
    image_boxes: list[Box],
    page_w: float,
    page_h: float,
) -> bool:
    """Detect a carrier signature column paired with an existing client stamp."""
    if anchor.phrase not in TRANSPORTER_NAME_TARGETS:
        return False
    if not (page_h * 0.10 < anchor.box.top < page_h * 0.975):
        return False
    in_side_column = anchor.box.x0 > page_w * 0.52 or anchor.box.x1 < page_w * 0.48
    if not in_side_column:
        return False
    ref = reference_stamp_box(anchor, image_boxes)
    if ref is None:
        return False
    if ref.x1 < anchor.box.x0:
        gap = anchor.box.x0 - ref.x1
    else:
        gap = ref.x0 - anchor.box.x1
    return gap <= page_w * 0.35


def stamp_size_for_anchor(
    anchor: Anchor,
    page_w: float,
    page_h: float,
    requested_w: float,
    ratio: float,
    image_boxes: list[Box] | None = None,
) -> tuple[float, float]:
    """Choose a restrained PDF size and match a nearby client stamp when possible."""
    requested_w = max(70.0, requested_w)
    page_max_width = max(58.0, min(112.0, page_w * 0.21))
    base_width = min(max(requested_w, 82.0), 106.0, page_max_width)
    ref = None
    if (
        anchor.phrase in TRANSPORTER_NAME_TARGETS
        or anchor.phrase in SIGNATURE_LABEL_TARGETS
        or anchor.phrase in GENERIC_TARGETS
        or is_signature_block_anchor(anchor, page_w, page_h)
    ):
        ref = reference_stamp_box(anchor, image_boxes or [])
    if ref is not None:
        target_height = min(max(ref.height, 50.0), 70.0, page_h * 0.085)
        width = target_height / max(ratio, 0.1)
        min_width = min(78.0, page_max_width)
    elif anchor.phrase in SUPPLIER_SIGNATURE_TARGETS:
        width = min(base_width, 104.0)
        min_width = min(78.0, page_max_width)
    elif is_footer_anchor(anchor, page_w, page_h):
        width = min(base_width, 96.0)
        min_width = min(74.0, page_max_width)
    elif anchor.phrase in FOOTER_TARGETS:
        width = min(base_width, 98.0)
        min_width = min(74.0, page_max_width)
    else:
        width = min(base_width, 102.0)
        min_width = min(78.0, page_max_width)

    width = min(max(width, min_width), page_max_width)
    if ratio > 0 and width * ratio > min(72.0, page_h * 0.09):
        width = min(width, min(72.0, page_h * 0.09) / ratio)
    width = min(max(width, min(70.0, page_max_width)), page_max_width)
    return width, width * ratio


def placement_candidates(anchor: Anchor, page_w: float, page_h: float, stamp_w: float, stamp_h: float) -> list[tuple[str, Box]]:
    a = anchor.box
    gap = 12
    if anchor.phrase in SUPPLIER_SIGNATURE_TARGETS and looks_like_supplier_signature_heading(anchor.box, page_w, page_h):
        center_x = min(max((a.x0 + a.x1) / 2, page_w * 0.72), page_w - stamp_w / 2 - 30)
        x = center_x - stamp_w / 2
        below_top = a.bottom + 28
        return [
            ("under_supplier_signature_heading", Box(x, below_top, x + stamp_w, below_top + stamp_h)),
            ("under_supplier_signature_right", Box(page_w - stamp_w - 55, below_top, page_w - 55, below_top + stamp_h)),
            ("supplier_signature_lower", Box(x, below_top + 34, x + stamp_w, below_top + 34 + stamp_h)),
            ("supplier_signature_low_right", Box(page_w - stamp_w - 55, page_h - stamp_h - 112, page_w - 55, page_h - 112)),
        ]
    if is_footer_anchor(anchor, page_w, page_h):
        center_x = (a.x0 + a.x1) / 2
        preferred_x = center_x - (stamp_w / 2)
        preferred_top = a.top - stamp_h - 22
        right_column_x = page_w - stamp_w - 80
        right_of_name_x = a.x1 + 8
        right_of_name_top = a.top - stamp_h - 18
        return [
            ("above_footer_name", Box(preferred_x, preferred_top, preferred_x + stamp_w, preferred_top + stamp_h)),
            ("right_of_footer_name", Box(right_of_name_x, right_of_name_top, right_of_name_x + stamp_w, right_of_name_top + stamp_h)),
            ("above_footer_right", Box(right_column_x, preferred_top, right_column_x + stamp_w, preferred_top + stamp_h)),
            ("footer_column_center", Box(page_w * 0.64, preferred_top, page_w * 0.64 + stamp_w, preferred_top + stamp_h)),
            ("footer_slightly_higher", Box(preferred_x, preferred_top - 18, preferred_x + stamp_w, preferred_top - 18 + stamp_h)),
        ]
    if anchor.phrase in SIGNATURE_LABEL_TARGETS:
        centered_x = (a.x0 + a.x1) / 2 - (stamp_w / 2)
        preferred_top = a.top - stamp_h - 8
        right_x = min(a.x1 + gap, page_w - stamp_w - 28)
        left_x = min(max(a.x0, 28), page_w - stamp_w - 28)
        return [
            ("above_signature_label_center", Box(centered_x, preferred_top, centered_x + stamp_w, preferred_top + stamp_h)),
            ("above_signature_label_left", Box(left_x, preferred_top, left_x + stamp_w, preferred_top + stamp_h)),
            ("above_signature_label_right", Box(right_x, preferred_top, right_x + stamp_w, preferred_top + stamp_h)),
            ("right_of_signature_label", Box(right_x, a.top - stamp_h / 2, right_x + stamp_w, a.top - stamp_h / 2 + stamp_h)),
            ("higher_signature_label_center", Box(centered_x, preferred_top - 18, centered_x + stamp_w, preferred_top - 18 + stamp_h)),
        ]
    if anchor.phrase in CONFIRMATION_HEADING_TARGETS:
        left_x = min(max(a.x0, 28), page_w - stamp_w - 28)
        right_x = min(max(a.x1 + gap, 28), page_w - stamp_w - 28)
        below_top = a.bottom + 8
        return [
            ("under_confirmation_heading", Box(left_x, below_top, left_x + stamp_w, below_top + stamp_h)),
            ("right_of_confirmation_heading", Box(right_x, max(a.top - 8, 0), right_x + stamp_w, max(a.top - 8, 0) + stamp_h)),
            ("above_confirmation_heading", Box(left_x, a.top - stamp_h - 8, left_x + stamp_w, a.top - 8)),
            ("right_lower", Box(page_w - stamp_w - 55, max(a.bottom + 10, page_h * 0.58), page_w - 55, max(a.bottom + 10, page_h * 0.58) + stamp_h)),
        ]
    return [
        ("right_of_anchor", Box(a.x1 + gap, max(a.top - 18, 0), a.x1 + gap + stamp_w, max(a.top - 18, 0) + stamp_h)),
        ("below_anchor", Box(min(max(a.x0, 20), page_w - stamp_w - 20), a.bottom + gap, min(max(a.x0, 20), page_w - stamp_w - 20) + stamp_w, a.bottom + gap + stamp_h)),
        ("above_anchor", Box(min(max(a.x0, 20), page_w - stamp_w - 20), a.top - stamp_h - gap, min(max(a.x0, 20), page_w - stamp_w - 20) + stamp_w, a.top - gap)),
        ("right_lower", Box(page_w - stamp_w - 55, max(a.bottom + 10, page_h * 0.58), page_w - 55, max(a.bottom + 10, page_h * 0.58) + stamp_h)),
        ("middle_right", Box(page_w - stamp_w - 65, page_h * 0.52, page_w - 65, page_h * 0.52 + stamp_h)),
        ("bottom_right_safe", Box(page_w - stamp_w - 60, page_h - stamp_h - 90, page_w - 60, page_h - 90)),
    ]


def fallback_candidates(page_w: float, page_h: float, stamp_w: float, stamp_h: float) -> list[tuple[str, Box]]:
    right_margins = [24, 40, 60, 82, 110, 140]
    bottom_margins = [28, 44, 62, 84, 110, 145, 185, 220]
    out = []
    for yi, bottom_margin in enumerate(bottom_margins):
        y = page_h - stamp_h - bottom_margin
        for xi, right_margin in enumerate(right_margins):
            x = page_w - stamp_w - right_margin
            out.append((f"fallback_bottom_right_{yi}_{xi}", Box(x, y, x + stamp_w, y + stamp_h)))
    return out


def signature_block_text_bottom(anchor: Anchor, word_boxes: list[Box], page_w: float) -> float:
    bottom = anchor.box.bottom
    anchor_center = (anchor.box.x0 + anchor.box.x1) / 2
    column_pad = max(45.0, page_w * 0.18)
    for box in word_boxes:
        same_side = (
            box.x1 < page_w * 0.56
            if anchor_center < page_w * 0.50
            else box.x0 > page_w * 0.44
        )
        near_column = (
            box.x1 >= anchor.box.x0 - column_pad
            and box.x0 <= anchor.box.x1 + column_pad
        )
        # Include the company/contact lines immediately under the carrier name,
        # but never unrelated text farther down the page.
        in_signature_lines = (
            anchor.box.top - 10
            <= box.top
            <= anchor.box.bottom + 34
        )
        if same_side and near_column and in_signature_lines:
            bottom = max(bottom, box.bottom)
    return bottom


def explicit_signature_population(
    anchor: Anchor,
    word_boxes: list[Box],
    page_w: float,
    page_h: float,
) -> int:
    """Count printed fields below a signature label in the same page column."""
    anchor_on_left = (anchor.box.x0 + anchor.box.x1) / 2 < page_w * 0.50
    window_bottom = min(page_h - 12.0, anchor.box.bottom + page_h * 0.30)
    count = 0
    for box in word_boxes:
        if box.bottom <= anchor.box.bottom + 2 or box.top >= window_bottom:
            continue
        same_column = box.x1 < page_w * 0.52 if anchor_on_left else box.x0 > page_w * 0.48
        if same_column:
            count += 1
    return count


def select_explicit_signature_anchors(
    anchors: list[Anchor],
    word_boxes: list[list[Box]],
    page_sizes: list[tuple[float, float]],
    image_boxes: list[list[Box]] | None = None,
) -> list[Anchor] | None:
    """Choose one explicit signature/stamp cell on every page that has one."""
    candidates = [
        anchor
        for anchor in anchors
        if anchor.phrase in SIGNATURE_LABEL_TARGETS
        and anchor.box.top < page_sizes[anchor.page_index][1] * 0.96
        and looks_like_carrier_signature_label(
            norm(anchor.line_text),
            anchor.phrase,
            anchor.box,
            page_sizes[anchor.page_index][1],
        )
    ]
    if not candidates:
        return None

    company_sides = []
    for anchor in anchors:
        if anchor.phrase not in TRANSPORTER_NAME_TARGETS:
            continue
        page_w, _page_h = page_sizes[anchor.page_index]
        company_sides.append((anchor.box.x0 + anchor.box.x1) / 2 >= page_w * 0.50)
    preferred_right = None
    if company_sides:
        preferred_right = sum(company_sides) * 2 >= len(company_sides)

    by_page: dict[int, list[Anchor]] = {}
    for anchor in candidates:
        by_page.setdefault(anchor.page_index, []).append(anchor)

    selected = []
    for page_index in sorted(by_page):
        page_w, page_h = page_sizes[page_index]

        def candidate_rank(anchor: Anchor) -> tuple[float, float, float]:
            population = explicit_signature_population(
                anchor,
                word_boxes[page_index],
                page_w,
                page_h,
            )
            is_right = (anchor.box.x0 + anchor.box.x1) / 2 >= page_w * 0.50
            score = float(anchor.score * 2) - population * 14.0
            if preferred_right is not None and is_right == preferred_right:
                score += 35.0
            if anchor.phrase in OVERLAP_OK_SIGNATURE_TARGETS:
                score += 260.0

            for company in anchors:
                if (
                    company.page_index != page_index
                    or company.phrase not in TRANSPORTER_NAME_TARGETS
                    or is_carrier_identity_header(company, anchors, page_w, page_h)
                ):
                    continue
                company_right = (company.box.x0 + company.box.x1) / 2 >= page_w * 0.50
                vertical_distance = abs(
                    ((company.box.top + company.box.bottom) / 2)
                    - ((anchor.box.top + anchor.box.bottom) / 2)
                )
                if company_right == is_right and vertical_distance <= page_h * 0.24:
                    score += 560.0 - vertical_distance * 1.7

            for role in anchors:
                if role.page_index != page_index or role.phrase not in GENERIC_TARGETS:
                    continue
                role_right = (role.box.x0 + role.box.x1) / 2 >= page_w * 0.50
                vertical_distance = abs(role.box.top - anchor.box.top)
                if role_right == is_right and vertical_distance <= page_h * 0.18:
                    score += 220.0 - vertical_distance

            page_images = image_boxes[page_index] if image_boxes else []
            if reference_stamp_box(anchor, page_images) is not None:
                score += 240.0
            return (score, anchor.box.x0, -float(population))

        selected.append(max(by_page[page_index], key=candidate_rank))
    return selected


def choose_explicit_signature_candidate(
    anchor: Anchor,
    word_boxes: list[Box],
    page_w: float,
    page_h: float,
    stamp_w: float,
    stamp_h: float,
    page_image: Image.Image | None = None,
    image_boxes: list[Box] | None = None,
) -> tuple[str, Box]:
    """Place inside the carrier cell, respecting its caption and client stamp."""
    ratio = stamp_h / max(stamp_w, 1.0)
    ref = reference_stamp_box(anchor, image_boxes or [])
    best_any: tuple[str, Box, float, float] | None = None
    on_left = (anchor.box.x0 + anchor.box.x1) / 2 < page_w * 0.50
    column_left = 18.0 if on_left else page_w * 0.50 + 6.0
    column_right = page_w * 0.50 - 6.0 if on_left else page_w - 18.0
    allowed_label_boxes = [
        box
        for box in word_boxes
        if overlap_area(box, anchor.box) > 0
        or (
            abs(box.top - anchor.box.top) <= 5.0
            and ((box.x0 + box.x1) / 2 >= page_w * 0.50) == (not on_left)
        )
    ]
    unrelated_boxes = [box for box in word_boxes if box not in allowed_label_boxes]

    for scale in (1.0, 0.94, 0.88, 0.82, 0.76, 0.70):
        width = max(70.0, stamp_w * scale)
        height = width * ratio
        center_x = (column_left + column_right) / 2 - width / 2
        x_values = [
            min(max(anchor.box.x0, column_left + 5.0), column_right - width - 5.0),
            min(max(center_x, column_left + 5.0), column_right - width - 5.0),
        ]
        if ref is not None:
            mirrored_x = (
                ref.x0 + page_w * 0.50
                if not on_left
                else ref.x0 - page_w * 0.50
            )
            x_values.insert(
                0,
                min(max(mirrored_x, column_left + 5.0), column_right - width - 5.0),
            )

        top_values: list[tuple[str, float]] = []
        if ref is not None:
            top_values.append(
                (
                    "explicit_align_client_stamp",
                    (ref.top + ref.bottom) / 2 - height / 2,
                )
            )
        top_values.extend(
            [
                ("explicit_above_caption", anchor.box.top - height - 4.0),
                ("explicit_below_caption", anchor.box.bottom + 4.0),
                ("explicit_right_of_caption", anchor.box.top - height * 0.48),
                ("explicit_center_on_caption", anchor.box.top - height * 0.35),
            ]
        )
        candidates = []
        for x in x_values:
            for reason, top in top_values:
                candidate_x = x
                if reason == "explicit_right_of_caption":
                    candidate_x = min(
                        max(anchor.box.x1 + 10.0, column_left + 5.0),
                        column_right - width - 5.0,
                    )
                rect = clamp_rect(
                    Box(candidate_x, top, candidate_x + width, top + height),
                    page_w,
                    page_h,
                    margin=14,
                )
                candidates.append((reason, rect))

        scored = score_candidates(
            candidates,
            word_boxes,
            page_w,
            page_h,
            anchor,
            page_image,
        )
        reason_bonus = {
            "explicit_align_client_stamp": 620.0,
            "explicit_right_of_caption": 310.0,
            "explicit_above_caption": 250.0,
            "explicit_below_caption": 180.0,
            "explicit_center_on_caption": 80.0,
        }
        rescored = []
        for reason, rect, score, text_overlap in scored:
            unrelated_overlap = overlap_ratio(rect, unrelated_boxes)
            score += reason_bonus.get(reason, 0.0) - unrelated_overlap * 60000.0
            rescored.append((reason, rect, score, text_overlap))

        if anchor.phrase in OVERLAP_OK_SIGNATURE_TARGETS:
            safe = [
                item
                for item in rescored
                if overlap_ratio(item[1], unrelated_boxes) <= 0.012
            ]
        else:
            safe = [
                item
                for item in rescored
                if overlap_ratio(item[1], unrelated_boxes) <= 0.012
                and (
                    is_safe_rect(item[1], word_boxes, page_image, page_w, page_h)
                    or visual_ink_ratio(item[1], page_image, page_w, page_h) <= 0.085
                )
            ]
        if safe:
            reason, rect, _score, _overlap = max(safe, key=lambda item: item[2])
            return reason, rect
        attempt_best = max(rescored, key=lambda item: item[2])
        if best_any is None or attempt_best[2] > best_any[2]:
            best_any = attempt_best
    if best_any is None:
        return "explicit_signature_unavailable", clamp_rect(
            Box(anchor.box.x0, anchor.box.bottom + 6, anchor.box.x0 + stamp_w, anchor.box.bottom + 6 + stamp_h),
            page_w,
            page_h,
        )
    return f"explicit_unverified_{best_any[0]}", best_any[1]


def choose_tight_labeled_candidate(
    anchor: Anchor,
    anchors: list[Anchor],
    word_boxes: list[Box],
    page_w: float,
    page_h: float,
    stamp_w: float,
    stamp_h: float,
) -> tuple[str, Box]:
    """Stay beside a certain carrier label when the signature cell is tight."""
    ratio = stamp_h / max(stamp_w, 1.0)
    text_bottom = signature_block_text_bottom(anchor, word_boxes, page_w)
    anchor_center = (anchor.box.x0 + anchor.box.x1) / 2
    on_right = anchor_center >= page_w * 0.50

    nearby_anchor_boxes = [
        item.box
        for item in anchors
        if item.page_index == anchor.page_index
        and (
            item.phrase in GENERIC_TARGETS
            or item.phrase in TRANSPORTER_NAME_TARGETS
            or item.phrase in SIGNATURE_LABEL_TARGETS
            or item.phrase in CONFIRMATION_HEADING_TARGETS
        )
        and ((item.box.x0 + item.box.x1) / 2 >= page_w * 0.50) == on_right
        and item.box.top <= text_bottom + 12
        and item.box.bottom >= anchor.box.top - 70
    ]
    signature_allowance = 52.0 if anchor.phrase in SIGNATURE_LABEL_TARGETS else 0.0
    block_top = min(
        [anchor.box.top - signature_allowance, *[box.top for box in nearby_anchor_boxes]]
    )
    allowed_boxes = [
        box
        for box in word_boxes
        if ((box.x0 + box.x1) / 2 >= page_w * 0.50) == on_right
        and box.top >= block_top - 10
        and box.bottom <= text_bottom + 12
    ]
    unrelated_boxes = [box for box in word_boxes if box not in allowed_boxes]

    candidates: list[tuple[float, float, Box]] = []
    widths = []
    for width in (stamp_w, stamp_w * 0.92, stamp_w * 0.84, stamp_w * 0.76, 78.0, 70.0):
        width = min(max(width, 70.0), page_w * 0.21)
        if any(abs(width - existing) <= 0.5 for existing in widths):
            continue
        widths.append(width)
        height = width * ratio
        if on_right:
            x_options = (
                min(max(anchor.box.x0, page_w * 0.53), page_w - width - 14),
                page_w - width - 18,
                min(max(anchor_center - width / 2, page_w * 0.51), page_w - width - 14),
            )
        else:
            x_options = (
                max(14.0, min(anchor.box.x0, page_w * 0.49 - width)),
                18.0,
                max(14.0, anchor_center - width / 2),
            )
        ideal_top = (
            anchor.box.top - height * 0.58
            if anchor.phrase in SIGNATURE_LABEL_TARGETS
            else text_bottom + 2.0
        )
        top_options = (
            ideal_top,
            max(14.0, anchor.box.top - height * 0.40),
            max(14.0, anchor.box.top - height * 0.72),
            max(14.0, anchor.box.top - height + 4.0),
            min(max(14.0, ideal_top + 10.0), page_h - height - 14.0),
        )
        for x in x_options:
            for top in top_options:
                rect = clamp_rect(Box(x, top, x + width, top + height), page_w, page_h, margin=12)
                unrelated_overlap = overlap_ratio(rect, unrelated_boxes)
                carrier_overlap = overlap_ratio(rect, allowed_boxes)
                distance = math.hypot(rect.x0 - anchor.box.x0, rect.top - ideal_top)
                shrink_penalty = max(0.0, stamp_w - width)
                cost = (
                    unrelated_overlap * 140000.0
                    + carrier_overlap * 550.0
                    + distance * 0.9
                    + shrink_penalty * 0.7
                )
                candidates.append((cost, unrelated_overlap, rect))

    _cost, unrelated_overlap, rect = min(
        candidates,
        key=lambda item: (item[0], item[1], -item[2].x0),
    )
    reason = (
        "carrier_label_immediately_below"
        if rect.top >= text_bottom + 1.0
        else "carrier_label_controlled_overlap"
    )
    return reason, rect


def select_transporter_signature_anchors(
    anchors: list[Anchor],
    word_boxes: list[list[Box]],
    image_boxes: list[list[Box]],
    page_sizes: list[tuple[float, float]],
) -> list[Anchor] | None:
    """Return one carrier signature anchor for every page that contains one."""
    signature_anchors = []
    for anchor in anchors:
        page_w, page_h = page_sizes[anchor.page_index]
        if anchor.phrase in TRANSPORTER_NAME_TARGETS:
            if is_carrier_identity_header(anchor, anchors, page_w, page_h):
                continue
            verified = (
                is_signature_block_anchor(anchor, page_w, page_h)
                or is_paired_signature_anchor(
                    anchor,
                    image_boxes[anchor.page_index],
                    page_w,
                    page_h,
                )
                or is_split_transporter_name_pair(anchor, anchors, page_w, page_h)
                or is_verified_caraus_company_pair(anchor, anchors, page_w, page_h)
            )
        else:
            verified = is_standalone_carrier_role_anchor(
                anchor,
                word_boxes[anchor.page_index],
                page_w,
                page_h,
            )
        if verified:
            signature_anchors.append(anchor)
    page_indexes = {anchor.page_index for anchor in signature_anchors}
    if not page_indexes:
        return None

    anchors_by_page: dict[int, list[Anchor]] = {}
    for anchor in signature_anchors:
        anchors_by_page.setdefault(anchor.page_index, []).append(anchor)

    return [
        max(
            anchors_by_page[page_index],
            key=lambda item: anchor_rank(item, *page_sizes[page_index]),
        )
        for page_index in sorted(page_indexes)
    ]


def render_visual_pages(pdf_path: Path, page_indexes: set[int], dpi: int = 120) -> dict[int, Image.Image]:
    if convert_from_path is None:
        return {}

    rendered: dict[int, Image.Image] = {}
    for page_index in sorted(page_indexes):
        try:
            images = convert_from_path(
                str(pdf_path),
                dpi=dpi,
                first_page=page_index + 1,
                last_page=page_index + 1,
                fmt="png",
                thread_count=1,
            )
        except Exception:
            continue
        if images:
            rendered[page_index] = images[0].convert("RGB")
    return rendered


def visual_ink_ratio(rect: Box, page_image: Image.Image | None, page_w: float, page_h: float) -> float:
    if page_image is None or rect.area <= 0:
        return 0.0

    scale_x = page_image.width / max(page_w, 1.0)
    scale_y = page_image.height / max(page_h, 1.0)
    pad = 3.0
    left = max(0, int((rect.x0 + pad) * scale_x))
    top = max(0, int((rect.top + pad) * scale_y))
    right = min(page_image.width, int((rect.x1 - pad) * scale_x))
    bottom = min(page_image.height, int((rect.bottom - pad) * scale_y))
    if right <= left or bottom <= top:
        return 1.0

    gray = page_image.crop((left, top, right, bottom)).convert("L")
    hist = gray.histogram()
    total = sum(hist)
    if total <= 0:
        return 1.0

    dark = sum(hist[:232])
    medium = sum(hist[232:244])
    return (dark + medium * 0.35) / total


def is_safe_rect(
    rect: Box,
    word_boxes: list[Box],
    page_image: Image.Image | None,
    page_w: float,
    page_h: float,
) -> bool:
    if overlap_ratio(rect, word_boxes) > 0.012:
        return False
    if visual_ink_ratio(rect, page_image, page_w, page_h) > 0.022:
        return False
    return True


def choose_signature_block_candidate(
    anchor: Anchor,
    word_boxes: list[Box],
    image_boxes: list[Box],
    page_w: float,
    page_h: float,
    stamp_w: float,
    stamp_h: float,
    page_image: Image.Image | None = None,
) -> tuple[str, Box]:
    ref = reference_stamp_box(anchor, image_boxes)
    text_bottom = signature_block_text_bottom(anchor, word_boxes, page_w)
    best_any: tuple[str, Box, float, float] | None = None
    for scale in (1.0, 0.92, 0.84, 0.76, 0.68, 0.60, 0.52):
        width = max(58.0, stamp_w * scale)
        height = width * (stamp_h / max(stamp_w, 1.0))
        anchor_center = (anchor.box.x0 + anchor.box.x1) / 2
        if anchor_center < page_w * 0.50:
            center_x = max(anchor_center, width / 2 + 35)
        else:
            center_x = min(
                max(anchor_center, page_w * 0.64),
                page_w - width / 2 - 35,
            )
        x = center_x - width / 2
        preferred_top = text_bottom + 3
        if ref is not None:
            ref_centered_top = ref.top + (ref.height - height) / 2
            preferred_top = max(preferred_top, ref_centered_top)
        aligned_x = min(max(anchor.box.x0, 28), page_w - width - 28)
        candidates = []
        if ref is not None:
            aligned_top = max(text_bottom + 3, ref.top + (ref.height - height) / 2)
            candidates.extend(
                [
                    (
                        "signature_block_align_client_stamp",
                        Box(aligned_x, aligned_top, aligned_x + width, aligned_top + height),
                    ),
                    (
                        "below_signature_block_match_client",
                        Box(x, aligned_top, x + width, aligned_top + height),
                    ),
                    (
                        "signature_block_client_higher",
                        Box(
                            aligned_x,
                            max(text_bottom + 3, aligned_top - 10),
                            aligned_x + width,
                            max(text_bottom + 3, aligned_top - 10) + height,
                        ),
                    ),
                    (
                        "signature_block_client_lower",
                        Box(aligned_x, aligned_top + 10, aligned_x + width, aligned_top + 10 + height),
                    ),
                ]
            )
        else:
            candidates.extend(
                [
                    ("below_signature_block_match_client", Box(x, preferred_top, x + width, preferred_top + height)),
                    ("below_signature_block_aligned", Box(aligned_x, preferred_top, aligned_x + width, preferred_top + height)),
                    ("signature_block_lower", Box(x, preferred_top + 18, x + width, preferred_top + 18 + height)),
                ]
            )
        safe_candidates = [
            (reason, clamp_rect(rect, page_w, page_h))
            for reason, rect in candidates
        ]
        scored = score_candidates(safe_candidates, word_boxes, page_w, page_h, anchor, page_image)
        safe = [
            item for item in scored
            if is_safe_rect(item[1], word_boxes, page_image, page_w, page_h)
        ]
        if safe:
            reason, rect, _score, _overlap = max(safe, key=lambda item: item[2])
            return reason, rect
        attempt_best = max(scored, key=lambda item: item[2])
        if best_any is None or attempt_best[2] > best_any[2]:
            best_any = attempt_best
    assert best_any is not None
    return best_any[0], best_any[1]


def score_rect(rect: Box, word_boxes: list[Box], page_w: float, page_h: float, anchor: Anchor | None) -> float:
    text_overlap = overlap_ratio(rect, word_boxes)
    score = 1000.0 - (text_overlap * 9000.0)

    if rect.top < 15 or rect.bottom > page_h - 15 or rect.x0 < 15 or rect.x1 > page_w - 15:
        score -= 500
    if rect.bottom > page_h - 28:
        score -= 500
    if anchor is not None:
        dist = math.hypot(rect.x0 - anchor.box.x0, rect.top - anchor.box.top)
        score += anchor.score * 5
        score -= dist * 0.18
    else:
        if rect.top > page_h * 0.42:
            score += 120
        if rect.x0 > page_w * 0.45:
            score += 70
    return score


def score_rect_with_visual(
    rect: Box,
    word_boxes: list[Box],
    page_w: float,
    page_h: float,
    anchor: Anchor | None,
    page_image: Image.Image | None = None,
) -> float:
    score = score_rect(rect, word_boxes, page_w, page_h, anchor)
    ink = visual_ink_ratio(rect, page_image, page_w, page_h)
    if ink > 0.008:
        score -= ink * 18000.0
    return score


def choose_best_candidate(
    candidates: list[tuple[str, Box]],
    word_boxes: list[Box],
    page_w: float,
    page_h: float,
    anchor: Anchor | None,
    page_image: Image.Image | None = None,
) -> tuple[str, Box]:
    scored = score_candidates(candidates, word_boxes, page_w, page_h, anchor, page_image)
    safe = [
        item for item in scored
        if is_safe_rect(item[1], word_boxes, page_image, page_w, page_h)
    ]
    pool = safe or scored
    best_reason, best_rect, _score, _text_overlap = max(pool, key=lambda item: item[2])
    return best_reason, best_rect


def score_candidates(
    candidates: list[tuple[str, Box]],
    word_boxes: list[Box],
    page_w: float,
    page_h: float,
    anchor: Anchor | None,
    page_image: Image.Image | None = None,
) -> list[tuple[str, Box, float, float]]:
    reason_bonus = {
        "below_signature_block_match_client": 220.0,
        "signature_block_align_client_stamp": 340.0,
        "signature_block_client_higher": 230.0,
        "signature_block_client_lower": 170.0,
        "below_signature_block_right": 170.0,
        "signature_block_lower": 80.0,
        "above_signature_label_center": 180.0,
        "above_signature_label_left": 140.0,
        "above_signature_label_right": 120.0,
        "higher_signature_label_center": 80.0,
        "under_confirmation_heading": 150.0,
        "right_of_confirmation_heading": 120.0,
        "above_footer_name": 180.0,
        "right_of_footer_name": 100.0,
        "above_footer_right": 80.0,
    }
    return [
        (
            reason,
            rect,
            score_rect_with_visual(rect, word_boxes, page_w, page_h, anchor, page_image) + reason_bonus.get(reason, 0.0),
            overlap_ratio(rect, word_boxes),
        )
        for reason, rect in candidates
    ]


def choose_footer_candidate(
    anchor: Anchor,
    word_boxes: list[Box],
    page_w: float,
    page_h: float,
    stamp_w: float,
    stamp_ratio: float,
    page_image: Image.Image | None = None,
) -> tuple[str, Box]:
    attempts = [stamp_w, stamp_w * 0.86, stamp_w * 0.72, stamp_w * 0.58]
    best_any: tuple[str, Box, float, float] | None = None
    for width in attempts:
        width = max(52.0, width)
        height = width * stamp_ratio
        candidates = [
            (reason, clamp_rect(rect, page_w, page_h))
            for reason, rect in placement_candidates(anchor, page_w, page_h, width, height)
        ]
        scored = score_candidates(candidates, word_boxes, page_w, page_h, anchor, page_image)
        safe = [
            item for item in scored
            if is_safe_rect(item[1], word_boxes, page_image, page_w, page_h)
        ]
        if safe:
            preferred = [item for item in safe if item[0] == "above_footer_name"]
            if preferred:
                reason, rect, _score, _overlap = max(preferred, key=lambda item: item[2])
                return reason, rect
            reason, rect, _score, _overlap = max(safe, key=lambda item: item[2])
            return reason, rect
        attempt_best = max(scored, key=lambda item: item[2])
        if best_any is None or attempt_best[2] > best_any[2]:
            best_any = attempt_best
    assert best_any is not None
    return best_any[0], best_any[1]


def choose_fallback_page_candidate(
    word_boxes: list[Box],
    page_w: float,
    page_h: float,
    stamp_w: float,
    stamp_ratio: float,
    page_image: Image.Image | None = None,
) -> tuple[str, Box] | None:
    attempts = [stamp_w, stamp_w * 0.92, stamp_w * 0.84, stamp_w * 0.76, stamp_w * 0.68]
    best_any: tuple[str, Box, float, float] | None = None
    for width in attempts:
        width = max(64.0, width)
        height = width * stamp_ratio
        candidates = [
            (reason, clamp_rect(rect, page_w, page_h))
            for reason, rect in fallback_candidates(page_w, page_h, width, height)
        ]
        scored = score_candidates(candidates, word_boxes, page_w, page_h, None, page_image)
        safe = [
            item for item in scored
            if is_safe_rect(item[1], word_boxes, page_image, page_w, page_h)
        ]
        if safe:
            reason, rect, _score, _overlap = max(
                safe,
                key=lambda item: (
                    item[2]
                    - (page_h - item[1].bottom) * 0.90
                    - (page_w - item[1].x1) * 0.50
                ),
            )
            return reason, rect
        attempt_best = max(scored, key=lambda item: item[2])
        if best_any is None or attempt_best[2] > best_any[2]:
            best_any = attempt_best
    if best_any is None:
        return None
    return best_any[0], best_any[1]


def choose_placements(
    anchors: list[Anchor],
    word_boxes: list[list[Box]],
    image_boxes: list[list[Box]],
    page_sizes: list[tuple[float, float]],
    stamp_w: float,
    stamp_ratio: float,
    allow_fallback: bool = False,
    visual_pages: dict[int, Image.Image] | None = None,
) -> list[Placement]:
    placements: list[Placement] = []
    page_count = len(page_sizes)
    explicit_signature_anchors = select_explicit_signature_anchors(
        anchors,
        word_boxes,
        page_sizes,
        image_boxes,
    )
    if explicit_signature_anchors:
        for anchor in explicit_signature_anchors:
            page_index = anchor.page_index
            page_w, page_h = page_sizes[page_index]
            page_image = (visual_pages or {}).get(page_index)
            page_stamp_w, page_stamp_h = stamp_size_for_anchor(
                anchor,
                page_w,
                page_h,
                stamp_w,
                stamp_ratio,
                image_boxes[page_index],
            )
            best_reason, best_rect = choose_explicit_signature_candidate(
                anchor,
                word_boxes[page_index],
                page_w,
                page_h,
                page_stamp_w,
                page_stamp_h,
                page_image,
                image_boxes[page_index],
            )
            if best_reason.startswith("explicit_unverified_"):
                best_reason, best_rect = choose_tight_labeled_candidate(
                    anchor,
                    anchors,
                    word_boxes[page_index],
                    page_w,
                    page_h,
                    page_stamp_w,
                    page_stamp_h,
                )
            placements.append(
                Placement(
                    page_index=page_index,
                    rect=best_rect,
                    score=score_rect(best_rect, word_boxes[page_index], page_w, page_h, anchor),
                    anchor_phrase=anchor.phrase,
                    reason=best_reason,
                )
            )

    selected_signature_anchors = select_transporter_signature_anchors(
        anchors,
        word_boxes,
        image_boxes,
        page_sizes,
    )
    if selected_signature_anchors and placements:
        explicit_pages = {placement.page_index for placement in placements}
        selected_signature_anchors = [
            anchor
            for anchor in selected_signature_anchors
            if anchor.page_index not in explicit_pages
        ]

    # A dedicated label on one page must not hide a verified carrier block on
    # another page of the same order.
    if placements and not selected_signature_anchors:
        return sorted(placements, key=lambda placement: placement.page_index)

    # A company name, tax ID, or generic transport word elsewhere in the
    # document is not a reliable signing location. When fallback is enabled,
    # only an explicit signature label or a verified carrier signature block
    # may select individual pages. Otherwise stamp every page in the safest
    # bottom-right area instead of guessing from a weak anchor.
    if not placements and not selected_signature_anchors and page_count and allow_fallback:
        for page_index in range(page_count):
            page_w, page_h = page_sizes[page_index]
            page_stamp_w = min(max(stamp_w, 78.0), 102.0, page_w * 0.18)
            best = choose_fallback_page_candidate(
                word_boxes[page_index],
                page_w,
                page_h,
                page_stamp_w,
                stamp_ratio,
                (visual_pages or {}).get(page_index),
            )
            if best is None:
                continue
            best_reason, best_rect = best
            placements.append(
                Placement(
                    page_index=page_index,
                    rect=best_rect,
                    score=score_rect(best_rect, word_boxes[page_index], page_w, page_h, None),
                    anchor_phrase="FALLBACK_EACH_PAGE",
                    reason=best_reason,
                )
            )
        return placements

    high_anchors = selected_signature_anchors or [a for a in anchors if a.score >= 95]
    if not high_anchors:
        high_anchors = sorted(anchors, key=lambda a: a.score, reverse=True)[:1]

    by_page: dict[int, Anchor] = {}
    for anchor in sorted(
        high_anchors,
        key=lambda a: anchor_rank(a, *page_sizes[a.page_index]),
        reverse=True,
    ):
        by_page.setdefault(anchor.page_index, anchor)

    for page_index, anchor in sorted(by_page.items(), key=lambda kv: kv[0]):
        page_w, page_h = page_sizes[page_index]
        page_image = (visual_pages or {}).get(page_index)
        page_stamp_w, page_stamp_h = stamp_size_for_anchor(
            anchor,
            page_w,
            page_h,
            stamp_w,
            stamp_ratio,
            image_boxes[page_index],
        )
        is_selected_signature = bool(
            selected_signature_anchors
            and anchor in selected_signature_anchors
        )
        if is_signature_block_anchor(anchor, page_w, page_h) or is_selected_signature:
            best_reason, best_rect = choose_signature_block_candidate(
                anchor,
                word_boxes[page_index],
                image_boxes[page_index],
                page_w,
                page_h,
                page_stamp_w,
                page_stamp_h,
                page_image,
            )
        elif is_footer_anchor(anchor, page_w, page_h):
            best_reason, best_rect = choose_footer_candidate(
                anchor,
                word_boxes[page_index],
                page_w,
                page_h,
                page_stamp_w,
                stamp_ratio,
                page_image,
            )
        else:
            candidates = [
                (reason, clamp_rect(rect, page_w, page_h))
                for reason, rect in placement_candidates(anchor, page_w, page_h, page_stamp_w, page_stamp_h)
            ]
            best_reason, best_rect = choose_best_candidate(
                candidates,
                word_boxes[page_index],
                page_w,
                page_h,
                anchor,
                page_image,
            )
        if not is_safe_rect(best_rect, word_boxes[page_index], page_image, page_w, page_h):
            if not is_selected_signature:
                continue
            best_reason, best_rect = choose_tight_labeled_candidate(
                anchor,
                anchors,
                word_boxes[page_index],
                page_w,
                page_h,
                page_stamp_w,
                page_stamp_h,
            )
        placements.append(
            Placement(
                page_index=page_index,
                rect=best_rect,
                score=score_rect(best_rect, word_boxes[page_index], page_w, page_h, anchor),
                anchor_phrase=anchor.phrase,
                reason=best_reason,
            )
        )

    # Never return a partially confirmed document when several carrier
    # signature pages were detected. Make will route an empty result to review.
    if selected_signature_anchors and not {
        anchor.page_index for anchor in selected_signature_anchors
    }.issubset({placement.page_index for placement in placements}):
        return []

    if not placements and page_count and allow_fallback:
        for page_index in range(page_count):
            page_w, page_h = page_sizes[page_index]
            page_stamp_w = min(max(stamp_w, 78.0), 102.0, page_w * 0.18)
            best = choose_fallback_page_candidate(
                word_boxes[page_index],
                page_w,
                page_h,
                page_stamp_w,
                stamp_ratio,
                (visual_pages or {}).get(page_index),
            )
            if best is None:
                continue
            best_reason, best_rect = best
            placements.append(
                Placement(
                    page_index=page_index,
                    rect=best_rect,
                    score=score_rect(best_rect, word_boxes[page_index], page_w, page_h, None),
                    anchor_phrase="FALLBACK_EACH_PAGE",
                    reason=best_reason,
                )
            )
    return sorted(placements, key=lambda placement: placement.page_index)


def stamp_pdf(
    input_pdf: Path,
    stamp_image: Path,
    output_pdf: Path,
    stamp_width: float = 175.0,
    allow_fallback: bool = False,
) -> dict:
    with Image.open(stamp_image) as img:
        ratio = img.height / max(img.width, 1)

    anchors, word_boxes, image_boxes, page_sizes = find_anchors(input_pdf)
    visual_page_indexes = {anchor.page_index for anchor in anchors}
    if allow_fallback and page_sizes:
        visual_page_indexes.update(range(len(page_sizes)))
    visual_pages = render_visual_pages(input_pdf, visual_page_indexes)
    placements = choose_placements(
        anchors,
        word_boxes,
        image_boxes,
        page_sizes,
        stamp_width,
        ratio,
        allow_fallback=allow_fallback,
        visual_pages=visual_pages,
    )

    reader = PdfReader(str(input_pdf))
    writer = PdfWriter()
    stamp_reader = ImageReader(str(stamp_image))

    placement_by_page = {p.page_index: p for p in placements}
    for page_index, page in enumerate(reader.pages):
        if page_index not in placement_by_page:
            writer.add_page(page)
            continue

        page = writer.add_page(page)

        # pdfplumber and OCR report coordinates in the page's visible, upright
        # orientation. Normalize PDF /Rotate into the page content before
        # merging the overlay so those coordinates remain true on 90/180/270
        # degree scanned pages as well.
        if page.rotation:
            page.transfer_rotation_to_content()

        page_w = float(page.mediabox.width)
        page_h = float(page.mediabox.height)
        p = placement_by_page[page_index]
        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=(page_w, page_h))
        x = p.rect.x0
        y = page_h - p.rect.bottom
        c.drawImage(stamp_reader, x, y, width=p.rect.width, height=p.rect.height, mask="auto")
        c.save()
        packet.seek(0)
        overlay = PdfReader(packet).pages[0]
        page.merge_page(overlay)

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    with output_pdf.open("wb") as f:
        writer.write(f)

    return {
        "input": str(input_pdf),
        "output": str(output_pdf),
        "placements": [
            {
                "page": p.page_index + 1,
                "rect_top_left": {
                    "x0": round(p.rect.x0, 2),
                    "top": round(p.rect.top, 2),
                    "x1": round(p.rect.x1, 2),
                    "bottom": round(p.rect.bottom, 2),
                },
                "score": round(p.score, 2),
                "anchor": p.anchor_phrase,
                "reason": p.reason,
            }
            for p in placements
        ],
        "anchor_count": len(anchors),
        "stamped": bool(placements),
        "needs_review": not bool(placements),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="DEGEI deterministic PDF stamp engine")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--stamp", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stamp-width", type=float, default=175.0)
    parser.add_argument("--allow-fallback", action="store_true")
    args = parser.parse_args()

    result = stamp_pdf(args.input, args.stamp, args.output, args.stamp_width, args.allow_fallback)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
