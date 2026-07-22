from __future__ import annotations

import argparse
import io
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pdfplumber
from PIL import Image
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
    ("TRANSPORTATOR CONFIRMARE", 130),
    ("CONFIRMARE TRANSPORTATOR", 120),
    ("CONFIRMAREA TRANSPORTATORULUI", 120),
    ("SEMNATURA SI STAMPILA", 115),
    ("SEMNATURA SI SEMNATURA", 100),
    ("SEMNATURA TRANSPORTATOR", 115),
    ("STAMPILA TRANSPORTATOR", 115),
    ("TRANSPORTATOR", 90),
    ("CARRIER", 90),
    ("HAULIER", 90),
    ("VETTORE", 90),
    ("SIGNATURE AND STAMP", 110),
    ("SIGN AND STAMP", 100),
    ("DEGEI LOGISTIC", 75),
    ("RO36256981", 70),
    ("FURNIZOR", 65),
    ("PRESTATOR", 65),
    ("SUBCONTRACTANT", 65),
]

GENERIC_TARGETS = {"TRANSPORTATOR", "CARRIER", "HAULIER", "VETTORE"}
FOOTER_TARGETS = {"DEGEI LOGISTIC", "RO36256981"}
TRANSPORTER_NAME_TARGETS = {"DEGEI LOGISTIC", "RO36256981"}
SUPPLIER_SIGNATURE_TARGETS = {"FURNIZOR", "PRESTATOR", "SUBCONTRACTANT"}
SIGNATURE_LABEL_TARGETS = {
    "SEMNATURA SI STAMPILA",
    "SEMNATURA SI SEMNATURA",
    "STAMPILA TRANSPORTATOR",
    "SIGNATURE AND STAMP",
    "SIGN AND STAMP",
}
CONFIRMATION_HEADING_TARGETS = {
    "CARRIER CONFIRMATION",
    "HAULIER CONFIRMATION",
    "TRANSPORTATOR CONFIRMARE",
    "CONFIRMARE TRANSPORTATOR",
    "CONFIRMAREA TRANSPORTATORULUI",
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


def phrase_box(line_words: list[dict], phrase: str) -> Box | None:
    norm_words = [norm(str(w.get("text", ""))) for w in line_words]
    phrase_words = phrase.split()
    for start in range(0, len(norm_words) - len(phrase_words) + 1):
        if norm_words[start : start + len(phrase_words)] == phrase_words:
            matched = line_words[start : start + len(phrase_words)]
            return Box(
                min(float(w["x0"]) for w in matched),
                min(float(w["top"]) for w in matched),
                max(float(w["x1"]) for w in matched),
                max(float(w["bottom"]) for w in matched),
            )
    return None


def find_anchors(pdf_path: Path) -> tuple[list[Anchor], list[list[Box]], list[list[Box]], list[tuple[float, float]]]:
    anchors: list[Anchor] = []
    all_word_boxes: list[list[Box]] = []
    all_image_boxes: list[list[Box]] = []
    page_sizes: list[tuple[float, float]] = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_index, page in enumerate(pdf.pages):
            page_sizes.append((float(page.width), float(page.height)))
            words = page.extract_words(
                x_tolerance=2,
                y_tolerance=3,
                keep_blank_chars=False,
                use_text_flow=False,
            )
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
                        if phrase in SIGNATURE_LABEL_TARGETS and (
                            len(line_norm) > 55 or line["box"].width > page.width * 0.55
                        ):
                            continue
                        # Generic words appear often in legal/payment paragraphs. They are valid
                        # anchors only when they look like a short field label, not body text.
                        if phrase in GENERIC_TARGETS:
                            if len(line_norm) > 38:
                                continue
                            if line["box"].width > page.width * 0.42:
                                continue
                        anchor_box = phrase_box(line["words"], phrase) or line["box"]
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
            and 0.40 <= ratio <= 2.20
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
    # Make sends a pixel-like target width. Convert it to a professional PDF size
    # based on the signature zone so it matches client stamps instead of dominating them.
    requested_w = max(70.0, requested_w)
    ref = None
    if anchor.phrase in TRANSPORTER_NAME_TARGETS or is_signature_block_anchor(anchor, page_w, page_h):
        ref = reference_stamp_box(anchor, image_boxes or [])
    if ref is not None:
        # The production artwork is wide while its circular seal occupies only
        # part of that width. Match the visible seal to the client's seal, not
        # the full square image bounding box.
        target_height = min(max(ref.height * 0.42, 38.0), 54.0, page_h * 0.075)
        lower_width = max(88.0, requested_w * 0.88)
        upper_width = min(125.0, requested_w * 1.08, page_w * 0.22)
        width = min(max(target_height / max(ratio, 0.1), lower_width), upper_width)
        max_height = 56.0
        min_width = min(lower_width, page_w * 0.20)
    elif anchor.phrase in SUPPLIER_SIGNATURE_TARGETS:
        width = min(requested_w, 118.0, page_w * 0.20)
        max_height = 58.0
        min_width = 82.0
    elif is_footer_anchor(anchor, page_w, page_h):
        width = min(requested_w, 96.0, page_w * 0.16)
        max_height = 46.0
        min_width = 56.0
    elif anchor.phrase in FOOTER_TARGETS:
        width = min(requested_w, 96.0, page_w * 0.16)
        max_height = 46.0
        min_width = 56.0
    else:
        width = min(requested_w, 112.0, page_w * 0.19)
        max_height = 54.0
        min_width = 70.0
    if ratio > 0:
        width = min(width, max_height / ratio)
    width = max(min_width, width)
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


def fallback_candidates(page_…1815 tokens truncated…      )
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
    attempts = [stamp_w, stamp_w * 0.90, stamp_w * 0.80, stamp_w * 0.70, stamp_w * 0.60]
    best_any: tuple[str, Box, float, float] | None = None
    for width in attempts:
        width = max(52.0, width)
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
            reason, rect, _score, _overlap = max(safe, key=lambda item: item[2])
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
            )
            if not is_safe_rect(best_rect, word_boxes[page_index], page_image, page_w, page_h):
                return []
            placements.append(
                Placement(
                    page_index=page_index,
                    rect=best_rect,
                    score=score_rect(best_rect, word_boxes[page_index], page_w, page_h, anchor),
                    anchor_phrase=anchor.phrase,
                    reason=best_reason,
                )
            )
        return placements

    selected_signature_anchors = select_transporter_signature_anchors(
        anchors,
        word_boxes,
        image_boxes,
        page_sizes,
    )
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
            continue
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
    if selected_signature_anchors and len(placements) != len(selected_signature_anchors):
        return []

    if not placements and page_count and allow_fallback:
        for page_index in range(page_count):
            page_w, page_h = page_sizes[page_index]
            page_stamp_w = min(max(stamp_w, 78.0), 112.0, page_w * 0.19)
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
        writer.add_page(page)

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

