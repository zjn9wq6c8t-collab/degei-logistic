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
    ("STAMPILA SI SEMNATURA TRANSPORTATORULUI", 170),
    ("SEMNATURA SI STAMPILA TRANSPORTATORULUI", 170),
    ("STAMPILA SI SEMNATURA TRANSPORTATOR", 165),
    ("SEMNATURA SI STAMPILA TRANSPORTATOR", 165),
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
    "STAMPILA SI SEMNATURA TRANSPORTATORULUI",
    "SEMNATURA SI STAMPILA TRANSPORTATORULUI",
    "STAMPILA SI SEMNATURA TRANSPORTATOR",
    "SEMNATURA SI STAMPILA TRANSPORTATOR",
    "SEMNATURA SI STAMPILA",
    "SEMNATURA SI SEMNATURA",
    "STAMPILA TRANSPORTATOR",
    "SIGNATURE AND STAMP",
    "SIGN AND STAMP",
}
OVERLAP_OK_SIGNATURE_TARGETS = {
    "STAMPILA SI SEMNATURA TRANSPORTATORULUI",
    "SEMNATURA SI STAMPILA TRANSPORTATORULUI",
    "STAMPILA SI SEMNATURA TRANSPORTATOR",
    "SEMNATURA SI STAMPILA TRANSPORTATOR",
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


def looks_like_carrier_signature_label(
    line_norm: str,
    phrase: str,
    box: Box,
    page_h: float,
) -> bool:
    """Reject legal prose and signature fields that belong to another party."""
    if phrase not in SIGNATURE_LABEL_TARGETS:
        return True
    if any(marker in line_norm for marker in NON_CARRIER_SIGNATURE_MARKERS):
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
                            if line["box"].width > page.width * 0.42:
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
            and 45 <= box.height <= 190ï®9¶‰žËkºwµçpÁ…•}¥µ…”°Á…•}Ü°Á…•} ¤(€€€¥˜¥¹¬€ø€À¸ÀÀàè(€€€€€€€Í½É”€´ô¥¹¬€¨€ÄàÀÀÀ¸À(€€€É•ÑÕÉ¸Í½É”(()‘•˜¡½½Í•}‰•ÍÑ}…¹‘¥‘…Ñ” (€€€…¹‘¥‘…Ñ•Ìè±¥ÍÑmÑÕÁ±•mÍÑÈ°	½áut°(€€€Ý½É‘}‰½á•Ìè±¥ÍÑm	½át°(€€€Á…•}Üè™±½…Ð°(€€€Á…•} è™±½…Ð°(€€€…¹¡½Èè¹¡½Èð9½¹”°(€€€Á…•}¥µ…”è%µ…”¹%µ…”ð9½¹”€ô9½¹”°(¤€´øÑÕÁ±•mÍÑÈ°	½átè(€€€Í½É•€ôÍ½É•}…¹‘¥‘…Ñ•Ì¡…¹‘¥‘…Ñ•Ì°Ý½É‘}‰½á•Ì°Á…•}Ü°Á…•} °…¹¡½È°Á…•}¥µ…”¤(€€€Í…™”€ôl(€€€€€€€¥Ñ•´™½È¥Ñ•´¥¸Í½É•(€€€€€€€¥˜¥Í}Í…™•}É•Ð¡¥Ñ•µlÅt°Ý½É‘}‰½á•Ì°Á…•}¥µ…”°Á…•}Ü°Á…•} ¤(€€€t(€€€Á½½°€ôÍ…™”½ÈÍ½É•(€€€‰•ÍÑ}É•…Í½¸°‰•ÍÑ}É•Ð°}Í½É”°}Ñ•áÑ}½Ù•É±…À€ôµ…à¡Á½½°°­•äõ±…µ‰‘„¥Ñ•´è¥Ñ•µlÉt¤(€€€É•ÑÕÉ¸‰•ÍÑ}É•…Í½¸°‰•ÍÑ}É•Ð(()‘•˜Í½É•}…¹‘¥‘…Ñ•Ì (€€€…¹‘¥‘…Ñ•Ìè±¥ÍÑmÑÕÁ±•mÍÑÈ°	½áut°(€€€Ý½É‘}‰½á•Ìè±¥ÍÑm	½át°(€€€Á…•}Üè™±½…Ð°(€€€Á…•} è™±½…Ð°(€€€…¹¡½Èè¹¡½Èð9½¹”°(€€€Á…•}¥µ…”è%µ…”¹%µ…”ð9½¹”€ô9½¹”°(¤€´ø±¥ÍÑmÑÕÁ±•mÍÑÈ°	½à°™±½…Ð°™±½…Ñutè(€€€É•…Í½¹}‰½¹ÕÌ€ôì(€€€€€€€€‰‰•±½Ý}Í¥¹…ÑÕÉ•}‰±½­}µ…Ñ¡}±¥•¹Ðˆè€ÈÈÀ¸À°(€€€€€€€€‰Í¥¹…ÑÕÉ•}‰±½­}…±¥¹}±¥•¹Ñ}ÍÑ…µÀˆè€ÌÐÀ¸À°(€€€€€€€€‰Í¥¹…ÑÕÉ•}‰±½­}±¥•¹Ñ}¡¥¡•Èˆè€ÈÌÀ¸À°(€€€€€€€€‰Í¥¹…ÑÕÉ•}‰±½­}±¥•¹Ñ}±½Ý•Èˆè€ÄÜÀ¸À°(€€€€€€€€‰‰•±½Ý}Í¥¹…ÑÕÉ•}‰±½­}É¥¡Ðˆè€ÄÜÀ¸À°(€€€€€€€€‰Í¥¹…ÑÕÉ•}‰±½­}±½Ý•Èˆè€àÀ¸À°(€€€€€€€€‰…‰½Ù•}Í¥¹…ÑÕÉ•}±…‰•±}•¹Ñ•Èˆè€ÄàÀ¸À°(€€€€€€€€‰…‰½Ù•}Í¥¹…ÑÕÉ•}±…‰•±}±•™Ðˆè€ÄÐÀ¸À°(€€€€€€€€‰…‰½Ù•}Í¥¹…ÑÕÉ•}±…‰•±}É¥¡Ðˆè€ÄÈÀ¸À°(€€€€€€€€‰¡¥¡•É}Í¥¹…ÑÕÉ•}±…‰•±}•¹Ñ•Èˆè€àÀ¸À°(€€€€€€€€‰Õ¹‘•É}½¹™¥Éµ…Ñ¥½¹}¡•…‘¥¹œˆè€ÄÔÀ¸À°(€€€€€€€€‰É¥¡Ñ}½™}½¹™¥Éµ…Ñ¥½¹}¡•…‘¥¹œˆè€ÄÈÀ¸À°(€€€€€€€€‰…‰½Ù•}™½½Ñ•É}¹…µ”ˆè€ÄàÀ¸À°(€€€€€€€€‰É¥¡Ñ}½™}™½½Ñ•É}¹…µ”ˆè€ÄÀÀ¸À°(€€€€€€€€‰…‰½Ù•}™½½Ñ•É}É¥¡Ðˆè€àÀ¸À°(€€€ô(€€€É•ÑÕÉ¸l(€€€€€€€€ (€€€€€€€€€€€É•…Í½¸°(€€€€€€€€€€€É•Ð°(€€€€€€€€€€€Í½É•}É•Ñ}Ý¥Ñ¡}Ù¥ÍÕ…°¡É•Ð°Ý½É‘}‰½á•Ì°Á…•}Ü°Á…•} °…¹¡½È°Á…•}¥µ…”¤€¬É•…Í½¹}‰½¹ÕÌ¹•Ð¡É•…Í½¸°€À¸À¤°(€€€€€€€€€€€½Ù•É±…Á}É…Ñ¥¼¡É•Ð°Ý½É‘}‰½á•Ì¤°(€€€€€€€€¤(€€€€€€€™½ÈÉ•…Í½¸°É•Ð¥¸…¹‘¥‘…Ñ•Ì(€€€t(()‘•˜¡½½Í•}™½½Ñ•É}…¹‘¥‘…Ñ” (€€€…¹¡½Èè¹¡½È°(€€€Ý½É‘}‰½á•Ìè±¥ÍÑm	½át°(€€€Á…•}Üè™±½…Ð°(€€€Á…•} è™±½…Ð°(€€€ÍÑ…µÁ}Üè™±½…Ð°(€€€ÍÑ…µÁ}É…Ñ¥¼è™±½…Ð°(€€€Á…•}¥µ…”è%µ…”¹%µ…”ð9½¹”€ô9½¹”°(¤€´øÑÕÁ±•mÍÑÈ°	½átè(€€€…ÑÑ•µÁÑÌ€ômÍÑ…µÁ}Ü°ÍÑ…µÁ}Ü€¨€À¸àØ°ÍÑ…µÁ}Ü€¨€À¸ÜÈ°ÍÑ…µÁ}Ü€¨€À¸Ôát(€€€‰•ÍÑ}…¹äèÑÕÁ±•mÍÑÈ°	½à°™±½…Ð°™±½…Ñtð9½¹”€ô9½¹”(€€€™½ÈÝ¥‘Ñ ¥¸…ÑÑ•µÁÑÌè(€€€€€€€Ý¥‘Ñ €ôµ…à ÔÈ¸À°Ý¥‘Ñ ¤(€€€€€€€¡•¥¡Ð€ôÝ¥‘Ñ €¨ÍÑ…µÁ}É…Ñ¥¼(€€€€€€€…¹‘¥‘…Ñ•Ì€ôl(€€€€€€€€€€€€¡É•…Í½¸°±…µÁ}É•Ð¡É•Ð°Á…•}Ü°Á…•} ¤¤(€€€€€€€€€€€™½ÈÉ•…Í½¸°É•Ð¥¸Á±…•µ•¹Ñ}…¹‘¥‘…Ñ•Ì¡…¹¡½È°Á…•}Ü°Á…•} °Ý¥‘Ñ °¡•¥¡Ð¤(€€€€€€€t(€€€€€€€Í½É•€ôÍ½É•}…¹‘¥‘…Ñ•Ì¡…¹‘¥‘…Ñ•Ì°Ý½É‘}‰½á•Ì°Á…•}Ü°Á…•} °…¹¡½È°Á…•}¥µ…”¤(€€€€€€€Í…™”€ôl(€€€€€€€€€€€¥Ñ•´™½È¥Ñ•´¥¸Í½É•(€€€€€€€€€€€¥˜¥Í}Í…™•}É•Ð¡¥Ñ•µlÅt°Ý½É‘}‰½á•Ì°Á…•}¥µ…”°Á…•}Ü°Á…•} ¤(€€€€€€€t(€€€€€€€¥˜Í…™”è(€€€€€€€€€€€ÁÉ•™•ÉÉ•€ôm¥Ñ•´™½È¥Ñ•´¥¸Í…™”¥˜¥Ñ•µlÁt€ôô€‰…‰½Ù•}™½½Ñ•É}¹…µ”‰t(€€€€€€€€€€€¥˜ÁÉ•™•ÉÉ•è(€€€€€€€€€€€€€€€É•…Í½¸°É•Ð°}Í½É”°}½Ù•É±…À€ôµ…à¡ÁÉ•™•ÉÉ•°­•äõ±…µ‰‘„¥Ñ•´è¥Ñ•µlÉt¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸É•…Í½¸°É•Ð(€€€€€€€€€€€É•…Í½¸°É•Ð°}Í½É”°}½Ù•É±…À€ôµ…à¡Í…™”°­•äõ±…µ‰‘„¥Ñ•´è¥Ñ•µlÉt¤(€€€€€€€€€€€É•ÑÕÉ¸É•…Í½¸°É•Ð(€€€€€€€…ÑÑ•µÁÑ}‰•ÍÐ€ôµ…à¡Í½É•°­•äõ±…µ‰‘„¥Ñ•´è¥Ñ•µlÉt¤(€€€€€€€¥˜‰•ÍÑ}…¹ä¥Ì9½¹”½È…ÑÑ•µÁÑ}‰•ÍÑlÉt€ø‰•ÍÑ}…¹ålÉtè(€€€€€€€€€€€‰•ÍÑ}…¹ä€ô…ÑÑ•µÁÑ}‰•ÍÐ(€€€…ÍÍ•ÉÐ‰•ÍÑ}…¹ä¥Ì¹½Ð9½¹”(€€€É•ÑÕÉ¸‰•ÍÑ}…¹ålÁt°‰•ÍÑ}…¹ålÅt(()‘•˜¡½½Í•}™…±±‰…­}Á…•}…¹‘¥‘…Ñ” (€€€Ý½É‘}‰½á•Ìè±¥ÍÑm	½át°(€€€Á…•}Üè™±½…Ð°(€€€Á…•} è™±½…Ð°(€€€ÍÑ…µÁ}Üè™±½…Ð°(€€€ÍÑ…µÁ}É…Ñ¥¼è™±½…Ð°(€€€Á…•}¥µ…”è%µ…”¹%µ…”ð9½¹”€ô9½¹”°(¤€´øÑÕÁ±•mÍÑÈ°	½átð9½¹”è(€€€…ÑÑ•µÁÑÌ€ômÍÑ…µÁ}Ü°ÍÑ…µÁ}Ü€¨€À¸äÀ°ÍÑ…µÁ}Ü€¨€À¸àÀ°ÍÑ…µÁ}Ü€¨€À¸ÜÀ°ÍÑ…µÁ}Ü€¨€À¸ØÁt(€€€‰•ÍÑ}…¹äèÑÕÁ±•mÍÑÈ°	½à°™±½…Ð°™±½…Ñtð9½¹”€ô9½¹”(€€€™½ÈÝ¥‘Ñ ¥¸…ÑÑ•µÁÑÌè(€€€€€€€Ý¥‘Ñ €ôµ…à ÔÈ¸À°Ý¥‘Ñ ¤(€€€€€€€¡•¥¡Ð€ôÝ¥‘Ñ €¨ÍÑ…µÁ}É…Ñ¥¼(€€€€€€€…¹‘¥‘…Ñ•Ì€ôl(€€€€€€€€€€€€¡É•…Í½¸°±…µÁ}É•Ð¡É•Ð°Á…•}Ü°Á…•} ¤¤(€€€€€€€€€€€™½ÈÉ•…Í½¸°É•Ð¥¸™…±±‰…­}…¹‘¥‘…Ñ•Ì¡Á…•}Ü°Á…•} °Ý¥‘Ñ °¡•¥¡Ð¤(€€€€€€€t(€€€€€€€Í½É•€ôÍ½É•}…¹‘¥‘…Ñ•Ì¡…¹‘¥‘…Ñ•Ì°Ý½É‘}‰½á•Ì°Á…•}Ü°Á…•} °9½¹”°Á…•}¥µ…”¤(€€€€€€€Í…™”€ôl(€€€€€€€€€€€¥Ñ•´™½È¥Ñ•´¥¸Í½É•(€€€€€€€€€€€¥˜¥Í}Í…™•}É•Ð¡¥Ñ•µlÅt°Ý½É‘}‰½á•Ì°Á…•}¥µ…”°Á…•}Ü°Á…•} ¤(€€€€€€€t(€€€€€€€¥˜Í…™”è(€€€€€€€€€€€É•…Í½¸°É•Ð°}Í½É”°}½Ù•É±…À€ôµ…à¡Í…™”°­•äõ±…µ‰‘„¥Ñ•´è¥Ñ•µlÉt¤(€€€€€€€€€€€É•ÑÕÉ¸É•…Í½¸°É•Ð(€€€€€€€…ÑÑ•µÁÑ}‰•ÍÐ€ôµ…à¡Í½É•°­•äõ±…µ‰‘„¥Ñ•´è¥Ñ•µlÉt¤(€€€€€€€¥˜‰•ÍÑ}…¹ä¥Ì9½¹”½È…ÑÑ•µÁÑ}‰•ÍÑlÉt€ø‰•ÍÑ}…¹ålÉtè(€€€€€€€€€€€‰•ÍÑ}…¹ä€ô…ÑÑ•µÁÑ}‰•ÍÐ(€€€¥˜‰•ÍÑ}…¹ä¥Ì9½¹”è(€€€€€€€É•ÑÕÉ¸9½¹”(€€€É•ÑÕÉ¸‰•ÍÑ}…¹ålÁt°‰•ÍÑ}…¹ålÅt(()‘•˜¡½½Í•}Á±…•µ•¹ÑÌ (€€€…¹¡½ÉÌè±¥ÍÑm¹¡½Ét°(€€€Ý½É‘}‰½á•Ìè±¥ÍÑm±¥ÍÑm	½áut°(€€€¥µ…•}‰½á•Ìè±¥ÍÑm±¥ÍÑm	½áut°(€€€Á…•}Í¥é•Ìè±¥ÍÑmÑÕÁ±•m™±½…Ð°™±½…Ñut°(€€€ÍÑ…µÁ}Üè™±½…Ð°(€€€ÍÑ…µÁ}É…Ñ¥¼è™±½…Ð°(€€€…±±½Ý}™…±±‰…¬è‰½½°€ô…±Í”°(€€€Ù¥ÍÕ…±}Á…•Ìè‘¥Ñm¥¹Ð°%µ…”¹%µ…•tð9½¹”€ô9½¹”°(¤€´ø±¥ÍÑmA±…•µ•¹Ñtè(€€€Á±…•µ•¹ÑÌè±¥ÍÑmA±…•µ•¹Ñt€ômt(€€€Á…•}½Õ¹Ð€ô±•¸¡Á…•}Í¥é•Ì¤(€€€•áÁ±¥¥Ñ}Í¥¹…ÑÕÉ•}…¹¡½ÉÌ€ôÍ•±•Ñ}•áÁ±¥¥Ñ}Í¥¹…ÑÕÉ•}…¹¡½ÉÌ (€€€€€€€…¹¡½ÉÌ°(€€€€€€€Ý½É‘}‰½á•Ì°(€€€€€€€Á…•}Í¥é•Ì°(€€€€¤(€€€¥˜•áÁ±¥¥Ñ}Í¥¹…ÑÕÉ•}…¹¡½ÉÌè(€€€€€€€™½È…¹¡½È¥¸•áÁ±¥¥Ñ}Í¥¹…ÑÕÉ•}…¹¡½ÉÌè(€€€€€€€€€€€Á…•}¥¹‘•à€ô…¹¡½È¹Á…•}¥¹‘•à(€€€€€€€€€€€Á…•}Ü°Á…•} €ôÁ…•}Í¥é•ÍmÁ…•}¥¹‘•át(€€€€€€€€€€€Á…•}¥µ…”€ô€¡Ù¥ÍÕ…±}Á…•Ì½Èíô¤¹•Ð¡Á…•}¥¹‘•à¤(€€€€€€€€€€€Á…•}ÍÑ…µÁ}Ü°Á…•}ÍÑ…µÁ} €ôÍÑ…µÁ}Í¥é•}™½É}…¹¡½È (€€€€€€€€€€€€€€€…¹¡½È°(€€€€€€€€€€€€€€€Á…•}Ü°(€€€€€€€€€€€€€€€Á…•} °(€€€€€€€€€€€€€€€ÍÑ…µÁ}Ü°(€€€€€€€€€€€€€€€ÍÑ…µÁ}É…Ñ¥¼°(€€€€€€€€€€€€€€€¥µ…•}‰½á•ÍmÁ…•}¥¹‘•át°(€€€€€€€€€€€€¤(€€€€€€€€€€€‰•ÍÑ}É•…Í½¸°‰•ÍÑ}É•Ð€ô¡½½Í•}•áÁ±¥¥Ñ}Í¥¹…ÑÕÉ•}…¹‘¥‘…Ñ” (€€€€€€€€€€€€€€€…¹¡½È°(€€€€€€€€€€€€€€€Ý½É‘}‰½á•ÍmÁ…•}¥¹‘•át°(€€€€€€€€€€€€€€€Á…•}Ü°(€€€€€€€€€€€€€€€Á…•} °(€€€€€€€€€€€€€€€Á…•}ÍÑ…µÁ}Ü°(€€€€€€€€€€€€€€€Á…•}ÍÑ…µÁ} °(€€€€€€€€€€€€€€€Á…•}¥µ…”°(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜€ (€€€€€€€€€€€€€€€…¹¡½È¹Á¡É…Í”¹½Ð¥¸=YI1A}=-}M%9QUI}QIQL(€€€€€€€€€€€€€€€…¹¹½Ð¥Í}Í…™•}É•Ð¡‰•ÍÑ}É•Ð°Ý½É‘}‰½á•ÍmÁ…•}¥¹‘•át°Á…•}¥µ…”°Á…•}Ü°Á…•} ¤(€€€€€€€€€€€€¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸mt(€€€€€€€€€€€Á±…•µ•¹ÑÌ¹…ÁÁ•¹ (€€€€€€€€€€€€€€€A±…•µ•¹Ð (€€€€€€€€€€€€€€€€€€€Á…•}¥¹‘•àõÁ…•}¥¹‘•à°(€€€€€€€€€€€€€€€€€€€É•Ðõ‰•ÍÑ}É•Ð°(€€€€€€€€€€€€€€€€€€€Í½É”õÍ½É•}É•Ð¡‰•ÍÑ}É•Ð°Ý½É‘}‰½á•ÍmÁ…•}¥¹‘•át°Á…•}Ü°Á…•} °…¹¡½È¤°(€€€€€€€€€€€€€€€€€€€…¹¡½É}Á¡É…Í”õ…¹¡½È¹Á¡É…Í”°(€€€€€€€€€€€€€€€€€€€É•…Í½¸õ‰•ÍÑ}É•…Í½¸°(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€¤(€€€€€€€É•ÑÕÉ¸Á±…•µ•¹ÑÌ((€€€Í•±•Ñ•‘}Í¥¹…ÑÕÉ•}…¹¡½ÉÌ€ôÍ•±•Ñ}ÑÉ…¹ÍÁ½ÉÑ•É}Í¥¹…ÑÕÉ•}…¹¡½ÉÌ (€€€€€€€…¹¡½ÉÌ°(€€€€€€€Ý½É‘}‰½á•Ì°(€€€€€€€¥µ…•}‰½á•Ì°(€€€€€€€Á…•}Í¥é•Ì°(€€€€¤((€€€€Œ½µÁ…¹ä¹…µ”°Ñ…à%°½È•¹•É¥ŒÑÉ…¹ÍÁ½ÉÐÝ½É•±Í•Ý¡•É”¥¸Ñ¡”(€€€€Œ‘½Õµ•¹Ð¥Ì¹½Ð„É•±¥…‰±”Í¥¹¥¹œ±½…Ñ¥½¸¸]¡•¸™…±±‰…¬¥Ì•¹…‰±•°(€€€€Œ½¹±ä…¸•áÁ±¥¥ÐÍ¥¹…ÑÕÉ”±…‰•°½È„Ù•É¥™¥•…ÉÉ¥•ÈÍ¥¹…ÑÕÉ”‰±½¬(€€€€Œµ…äÍ•±•Ð¥¹‘¥Ù¥‘Õ…°Á…•Ì¸=Ñ¡•ÉÝ¥Í”ÍÑ…µÀ•Ù•ÉäÁ…”¥¸Ñ¡”Í…™•ÍÐ(€€€€Œ‰½ÑÑ½´µÉ¥¡Ð…É•„¥¹ÍÑ•…½˜Õ•ÍÍ¥¹œ™É½´„Ý•…¬…¹¡½È¸(€€€¥˜¹½ÐÍ•±•Ñ•‘}Í¥¹…ÑÕÉ•}…¹¡½ÉÌ…¹Á…•}½Õ¹Ð…¹…±±½Ý}™…±±‰…¬è(€€€€€€€™½ÈÁ…•}¥¹‘•à¥¸É…¹”¡Á…•}½Õ¹Ð¤è(€€€€€€€€€€€Á…•}Ü°Á…•} €ôÁ…•}Í¥é•ÍmÁ…•}¥¹‘•át(€€€€€€€€€€€Á…•}ÍÑ…µÁ}Ü€ôµ¥¸¡µ…à¡ÍÑ…µÁ}Ü°€Üà¸À¤°€ÄÄÈ¸À°Á…•}Ü€¨€À¸Ää¤(€€€€€€€€€€€‰•ÍÐ€ô¡½½Í•}™…±±‰…­}Á…•}…¹‘¥‘…Ñ” (€€€€€€€€€€€€€€€Ý½É‘}‰½á•ÍmÁ…•}¥¹‘•át°(€€€€€€€€€€€€€€€Á…•}Ü°(€€€€€€€€€€€€€€€Á…•} °(€€€€€€€€€€€€€€€Á…•}ÍÑ…µÁ}Ü°(€€€€€€€€€€€€€€€ÍÑ…µÁ}É…Ñ¥¼°(€€€€€€€€€€€€€€€€¡Ù¥ÍÕ…±}Á…•Ì½Èíô¤¹•Ð¡Á…•}¥¹‘•à¤°(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜‰•ÍÐ¥Ì9½¹”è(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€‰•ÍÑ}É•…Í½¸°‰•ÍÑ}É•Ð€ô‰•ÍÐ(€€€€€€€€€€€Á±…•µ•¹ÑÌ¹…ÁÁ•¹ (€€€€€€€€€€€€€€€A±…•µ•¹Ð (€€€€€€€€€€€€€€€€€€€Á…•}¥¹‘•àõÁ…•}¥¹‘•à°(€€€€€€€€€€€€€€€€€€€É•Ðõ‰•ÍÑ}É•Ð°(€€€€€€€€€€€€€€€€€€€Í½É”õÍ½É•}É•Ð¡‰•ÍÑ}É•Ð°Ý½É‘}‰½á•ÍmÁ…•}¥¹‘•át°Á…•}Ü°Á…•} °9½¹”¤°(€€€€€€€€€€€€€€€€€€€…¹¡½É}Á¡É…Í”ô‰11	-}!}Aˆ°(€€€€€€€€€€€€€€€€€€€É•…Í½¸õ‰•ÍÑ}É•…Í½¸°(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€¤(€€€€€€€É•ÑÕÉ¸Á±…•µ•¹ÑÌ((€€€¡¥¡}…¹¡½ÉÌ€ôÍ•±•Ñ•‘}Í¥¹…ÑÕÉ•}…¹¡½ÉÌ½Èm„™½È„¥¸…¹¡½ÉÌ¥˜„¹Í½É”€øô€äÕt(€€€¥˜¹½Ð¡¥¡}…¹¡½ÉÌè(€€€€€€€¡¥¡}…¹¡½ÉÌ€ôÍ½ÉÑ•¡…¹¡½ÉÌ°­•äõ±…µ‰‘„„è„¹Í½É”°É•Ù•ÉÍ”õQÉÕ”¥lèÅt((€€€‰å}Á…”è‘¥Ñm¥¹Ð°¹¡½Ét€ôíô(€€€™½È…¹¡½È¥¸Í½ÉÑ• (€€€€€€€¡¥¡}…¹¡½ÉÌ°(€€€€€€€­•äõ±…µ‰‘„„è…¹¡½É}É…¹¬¡„°€©Á…•}Í¥é•Ím„¹Á…•}¥¹‘•át¤°(€€€€€€€É•Ù•ÉÍ”õQÉÕ”°(€€€€¤è(€€€€€€€‰å}Á…”¹Í•Ñ‘•™…Õ±Ð¡…¹¡½È¹Á…•}¥¹‘•à°…¹¡½È¤((€€€™½ÈÁ…•}¥¹‘•à°…¹¡½È¥¸Í½ÉÑ•¡‰å}Á…”¹¥Ñ•µÌ ¤°­•äõ±…µ‰‘„­Øè­ÙlÁt¤è(€€€€€€€Á…•}Ü°Á…•} €ôÁ…•}Í¥é•ÍmÁ…•}¥¹‘•át(€€€€€€€Á…•}¥µ…”€ô€¡Ù¥ÍÕ…±}Á…•Ì½Èíô¤¹•Ð¡Á…•}¥¹‘•à¤(€€€€€€€Á…•}ÍÑ…µÁ}Ü°Á…•}ÍÑ…µÁ} €ôÍÑ…µÁ}Í¥é•}™½É}…¹¡½È (€€€€€€€€€€€…¹¡½È°(€€€€€€€€€€€Á…•}Ü°(€€€€€€€€€€€Á…•} °(€€€€€€€€€€€ÍÑ…µÁ}Ü°(€€€€€€€€€€€ÍÑ…µÁ}É…Ñ¥¼°(€€€€€€€€€€€¥µ…•}‰½á•ÍmÁ…•}¥¹‘•át°(€€€€€€€€¤(€€€€€€€¥Í}Í•±•Ñ•‘}Í¥¹…ÑÕÉ”€ô‰½½° (€€€€€€€€€€€Í•±•Ñ•‘}Í¥¹…ÑÕÉ•}…¹¡½ÉÌ(€€€€€€€€€€€…¹…¹¡½È¥¸Í•±•Ñ•‘}Í¥¹…ÑÕÉ•}…¹¡½ÉÌ(€€€€€€€€¤(€€€€€€€¥˜¥Í}Í¥¹…ÑÕÉ•}‰±½­}…¹¡½È¡…¹¡½È°Á…•}Ü°Á…•} ¤½È¥Í}Í•±•Ñ•‘}Í¥¹…ÑÕÉ”è(€€€€€€€€€€€‰•ÍÑ}É•…Í½¸°‰•ÍÑ}É•Ð€ô¡½½Í•}Í¥¹…ÑÕÉ•}‰±½­}…¹‘¥‘…Ñ” (€€€€€€€€€€€€€€€…¹¡½È°(€€€€€€€€€€€€€€€Ý½É‘}‰½á•ÍmÁ…•}¥¹‘•át°(€€€€€€€€€€€€€€€¥µ…•}‰½á•ÍmÁ…•}¥¹‘•át°(€€€€€€€€€€€€€€€Á…•}Ü°(€€€€€€€€€€€€€€€Á…•} °(€€€€€€€€€€€€€€€Á…•}ÍÑ…µÁ}Ü°(€€€€€€€€€€€€€€€Á…•}ÍÑ…µÁ} °(€€€€€€€€€€€€€€€Á…•}¥µ…”°(€€€€€€€€€€€€¤(€€€€€€€•±¥˜¥Í}™½½Ñ•É}…¹¡½È¡…¹¡½È°Á…•}Ü°Á…•} ¤è(€€€€€€€€€€€‰•ÍÑ}É•…Í½¸°‰•ÍÑ}É•Ð€ô¡½½Í•}™½½Ñ•É}…¹‘¥‘…Ñ” (€€€€€€€€€€€€€€€…¹¡½È°(€€€€€€€€€€€€€€€Ý½É‘}‰½á•ÍmÁ…•}¥¹‘•át°(€€€€€€€€€€€€€€€Á…•}Ü°(€€€€€€€€€€€€€€€Á…•} °(€€€€€€€€€€€€€€€Á…•}ÍÑ…µÁ}Ü°(€€€€€€€€€€€€€€€ÍÑ…µÁ}É…Ñ¥¼°(€€€€€€€€€€€€€€€Á…•}¥µ…”°(€€€€€€€€€€€€¤(€€€€€€€•±Í”è(€€€€€€€€€€€…¹‘¥‘…Ñ•Ì€ôl(€€€€€€€€€€€€€€€€¡É•…Í½¸°±…µÁ}É•Ð¡É•Ð°Á…•}Ü°Á…•} ¤¤(€€€€€€€€€€€€€€€™½ÈÉ•…Í½¸°É•Ð¥¸Á±…•µ•¹Ñ}…¹‘¥‘…Ñ•Ì¡…¹¡½È°Á…•}Ü°Á…•} °Á…•}ÍÑ…µÁ}Ü°Á…•}ÍÑ…µÁ} ¤(€€€€€€€€€€€t(€€€€€€€€€€€‰•ÍÑ}É•…Í½¸°‰•ÍÑ}É•Ð€ô¡½½Í•}‰•ÍÑ}…¹‘¥‘…Ñ” (€€€€€€€€€€€€€€€…¹‘¥‘…Ñ•Ì°(€€€€€€€€€€€€€€€Ý½É‘}‰½á•ÍmÁ…•}¥¹‘•át°(€€€€€€€€€€€€€€€Á…•}Ü°(€€€€€€€€€€€€€€€Á…•} °(€€€€€€€€€€€€€€€…¹¡½È°(€€€€€€€€€€€€€€€Á…•}¥µ…”°(€€€€€€€€€€€€¤(€€€€€€€¥˜¹½Ð¥Í}Í…™•}É•Ð¡‰•ÍÑ}É•Ð°Ý½É‘}‰½á•ÍmÁ…•}¥¹‘•át°Á…•}¥µ…”°Á…•}Ü°Á…•} ¤è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€Á±…•µ•¹ÑÌ¹…ÁÁ•¹ (€€€€€€€€€€€A±…•µ•¹Ð (€€€€€€€€€€€€€€€Á…•}¥¹‘•àõÁ…•}¥¹‘•à°(€€€€€€€€€€€€€€€É•Ðõ‰•ÍÑ}É•Ð°(€€€€€€€€€€€€€€€Í½É”õÍ½É•}É•Ð¡‰•ÍÑ}É•Ð°Ý½É‘}‰½á•ÍmÁ…•}¥¹‘•át°Á…•}Ü°Á…•} °…¹¡½È¤°(€€€€€€€€€€€€€€€…¹¡½É}Á¡É…Í”õ…¹¡½È¹Á¡É…Í”°(€€€€€€€€€€€€€€€É•…Í½¸õ‰•ÍÑ}É•…Í½¸°(€€€€€€€€€€€€¤(€€€€€€€€¤((€€€€Œ9•Ù•ÈÉ•ÑÕÉ¸„Á…ÉÑ¥…±±ä½¹™¥Éµ•‘½Õµ•¹ÐÝ¡•¸Í•Ù•É…°…ÉÉ¥•È(€€€€ŒÍ¥¹…ÑÕÉ”Á…•ÌÝ•É”‘•Ñ•Ñ•¸5…­”Ý¥±°É½ÕÑ”…¸•µÁÑäÉ•ÍÕ±ÐÑ¼É•Ù¥•Ü¸(€€€¥˜Í•±•Ñ•‘}Í¥¹…ÑÕÉ•}…¹¡½ÉÌ…¹±•¸¡Á±…•µ•¹ÑÌ¤€„ô±•¸¡Í•±•Ñ•‘}Í¥¹…ÑÕÉ•}…¹¡½ÉÌ¤è(€€€€€€€É•ÑÕÉ¸mt((€€€¥˜¹½ÐÁ±…•µ•¹ÑÌ…¹Á…•}½Õ¹Ð…¹…±±½Ý}™…±±‰…¬è(€€€€€€€™½ÈÁ…•}¥¹‘•à¥¸É…¹”¡Á…•}½Õ¹Ð¤è(€€€€€€€€€€€Á…•}Ü°Á…•} €ôÁ…•}Í¥é•ÍmÁ…•}¥¹‘•át(€€€€€€€€€€€Á…•}ÍÑ…µÁ}Ü€ôµ¥¸¡µ…à¡ÍÑ…µÁ}Ü°€Üà¸À¤°€ÄÄÈ¸À°Á…•}Ü€¨€À¸Ää¤(€€€€€€€€€€€‰•ÍÐ€ô¡½½Í•}™…±±‰…­}Á…•}…¹‘¥‘…Ñ” (€€€€€€€€€€€€€€€Ý½É‘}‰½á•ÍmÁ…•}¥¹‘•át°(€€€€€€€€€€€€€€€Á…•}Ü°(€€€€€€€€€€€€€€€Á…•} °(€€€€€€€€€€€€€€€Á…•}ÍÑ…µÁ}Ü°(€€€€€€€€€€€€€€€ÍÑ…µÁ}É…Ñ¥¼°(€€€€€€€€€€€€€€€€¡Ù¥ÍÕ…±}Á…•Ì½Èíô¤¹•Ð¡Á…•}¥¹‘•à¤°(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜‰•ÍÐ¥Ì9½¹”è(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€‰•ÍÑ}É•…Í½¸°‰•ÍÑ}É•Ð€ô‰•ÍÐ(€€€€€€€€€€€Á±…•µ•¹ÑÌ¹…ÁÁ•¹ (€€€€€€€€€€€€€€€A±…•µ•¹Ð (€€€€€€€€€€€€€€€€€€€Á…•}¥¹‘•àõÁ…•}¥¹‘•à°(€€€€€€€€€€€€€€€€€€€É•Ðõ‰•ÍÑ}É•Ð°(€€€€€€€€€€€€€€€€€€€Í½É”õÍ½É•}É•Ð¡‰•ÍÑ}É•Ð°Ý½É‘}‰½á•ÍmÁ…•}¥¹‘•át°Á…•}Ü°Á…•} °9½¹”¤°(€€€€€€€€€€€€€€€€€€€…¹¡½É}Á¡É…Í”ô‰11	-}!}Aˆ°(€€€€€€€€€€€€€€€€€€€É•…Í½¸õ‰•ÍÑ}É•…Í½¸°(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€¤(€€€É•ÑÕÉ¸Á±…•µ•¹ÑÌ(()‘•˜ÍÑ…µÁ}Á‘˜ (€€€¥¹ÁÕÑ}Á‘˜èA…Ñ °(€€€ÍÑ…µÁ}¥µ…”èA…Ñ °(€€€½ÕÑÁÕÑ}Á‘˜èA…Ñ °(€€€ÍÑ…µÁ}Ý¥‘Ñ è™±½…Ð€ô€ÄÜÔ¸À°(€€€…±±½Ý}™…±±‰…¬è‰½½°€ô…±Í”°(¤€´ø‘¥Ðè(€€€Ý¥Ñ %µ…”¹½Á•¸¡ÍÑ…µÁ}¥µ…”¤…Ì¥µœè(€€€€€€€É…Ñ¥¼€ô¥µœ¹¡•¥¡Ð€¼µ…à¡¥µœ¹Ý¥‘Ñ °€Ä¤((€€€…¹¡½ÉÌ°Ý½É‘}‰½á•Ì°¥µ…•}‰½á•Ì°Á…•}Í¥é•Ì€ô™¥¹‘}…¹¡½ÉÌ¡¥¹ÁÕÑ}Á‘˜¤(€€€Ù¥ÍÕ…±}Á…•}¥¹‘•á•Ì€ôí…¹¡½È¹Á…•}¥¹‘•à™½È…¹¡½È¥¸…¹¡½ÉÍô(€€€¥˜…±±½Ý}™…±±‰…¬…¹Á…•}Í¥é•Ìè(€€€€€€€Ù¥ÍÕ…±}Á…•}¥¹‘•á•Ì¹ÕÁ‘…Ñ”¡É…¹”¡±•¸¡Á…•}Í¥é•Ì¤¤¤(€€€Ù¥ÍÕ…±}Á…•Ì€ôÉ•¹‘•É}Ù¥ÍÕ…±}Á…•Ì¡¥¹ÁÕÑ}Á‘˜°Ù¥ÍÕ…±}Á…•}¥¹‘•á•Ì¤(€€€Á±…•µ•¹ÑÌ€ô¡½½Í•}Á±…•µ•¹ÑÌ (€€€€€€€…¹¡½ÉÌ°(€€€€€€€Ý½É‘}‰½á•Ì°(€€€€€€€¥µ…•}‰½á•Ì°(€€€€€€€Á…•}Í¥é•Ì°(€€€€€€€ÍÑ…µÁ}Ý¥‘Ñ °(€€€€€€€É…Ñ¥¼°(€€€€€€€…±±½Ý}™…±±‰…¬õ…±±½Ý}™…±±‰…¬°(€€€€€€€Ù¥ÍÕ…±}Á…•ÌõÙ¥ÍÕ…±}Á…•Ì°(€€€€¤((€€€É•…‘•È€ôA‘™I•…‘•È¡ÍÑÈ¡¥¹ÁÕÑ}Á‘˜¤¤(€€€ÝÉ¥Ñ•È€ôA‘™]É¥Ñ•È ¤(€€€ÍÑ…µÁ}É•…‘•È€ô%µ…•I•…‘•È¡ÍÑÈ¡ÍÑ…µÁ}¥µ…”¤¤((€€€Á±…•µ•¹Ñ}‰å}Á…”€ôíÀ¹Á…•}¥¹‘•àèÀ™½ÈÀ¥¸Á±…•µ•¹ÑÍô(€€€™½ÈÁ…•}¥¹‘•à°Á…”¥¸•¹Õµ•É…Ñ”¡É•…‘•È¹Á…•Ì¤è(€€€€€€€¥˜Á…•}¥¹‘•à¹½Ð¥¸Á±…•µ•¹Ñ}‰å}Á…”è(€€€€€€€€€€€ÝÉ¥Ñ•È¹…‘‘}Á…”¡Á…”¤(€€€€€€€€€€€½¹Ñ¥¹Õ”((€€€€€€€Á…•}Ü€ô™±½…Ð¡Á…”¹µ•‘¥…‰½à¹Ý¥‘Ñ ¤(€€€€€€€Á…•} €ô™±½…Ð¡Á…”¹µ•‘¥…‰½à¹¡•¥¡Ð¤(€€€€€€€À€ôÁ±…•µ•¹Ñ}‰å}Á…•mÁ…•}¥¹‘•át(€€€€€€€Á…­•Ð€ô¥¼¹	åÑ•Í%< ¤(€€€€€€€Œ€ô…¹Ù…Ì¹…¹Ù…Ì¡Á…­•Ð°Á…•Í¥é”ô¡Á…•}Ü°Á…•} ¤¤(€€€€€€€à€ôÀ¹É•Ð¹àÀ(€€€€€€€ä€ôÁ…•} €´À¹É•Ð¹‰½ÑÑ½´(€€€€€€€Œ¹‘É…Ý%µ…”¡ÍÑ…µÁ}É•…‘•È°à°ä°Ý¥‘Ñ õÀ¹É•Ð¹Ý¥‘Ñ °¡•¥¡ÐõÀ¹É•Ð¹¡•¥¡Ð°µ…Í¬ô‰…ÕÑ¼ˆ¤(€€€€€€€Œ¹Í…Ù” ¤(€€€€€€€Á…­•Ð¹Í••¬ À¤(€€€€€€€½Ù•É±…ä€ôA‘™I•…‘•È¡Á…­•Ð¤¹Á…•ÍlÁt(€€€€€€€Á…”¹µ•É•}Á…”¡½Ù•É±…ä¤(€€€€€€€ÝÉ¥Ñ•È¹…‘‘}Á…”¡Á…”¤((€€€½ÕÑÁÕÑ}Á‘˜¹Á…É•¹Ð¹µ­‘¥È¡Á…É•¹ÑÌõQÉÕ”°•á¥ÍÑ}½¬õQÉÕ”¤(€€€Ý¥Ñ ½ÕÑÁÕÑ}Á‘˜¹½Á•¸ ‰Ýˆˆ¤…Ì˜è(€€€€€€€ÝÉ¥Ñ•È¹ÝÉ¥Ñ”¡˜¤((€€€É•ÑÕÉ¸ì(€€€€€€€€‰¥¹ÁÕÐˆèÍÑÈ¡¥¹ÁÕÑ}Á‘˜¤°(€€€€€€€€‰½ÕÑÁÕÐˆèÍÑÈ¡½ÕÑÁÕÑ}Á‘˜¤°(€€€€€€€€‰Á±…•µ•¹ÑÌˆèl(€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€‰Á…”ˆèÀ¹Á…•}¥¹‘•à€¬€Ä°(€€€€€€€€€€€€€€€€‰É•Ñ}Ñ½Á}±•™Ðˆèì(€€€€€€€€€€€€€€€€€€€€‰àÀˆèÉ½Õ¹¡À¹É•Ð¹àÀ°€È¤°(€€€€€€€€€€€€€€€€€€€€‰Ñ½ÀˆèÉ½Õ¹¡À¹É•Ð¹Ñ½À°€È¤°(€€€€€€€€€€€€€€€€€€€€‰àÄˆèÉ½Õ¹¡À¹É•Ð¹àÄ°€È¤°(€€€€€€€€€€€€€€€€€€€€‰‰½ÑÑ½´ˆèÉ½Õ¹¡À¹É•Ð¹‰½ÑÑ½´°€È¤°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€€‰Í½É”ˆèÉ½Õ¹¡À¹Í½É”°€È¤°(€€€€€€€€€€€€€€€€‰…¹¡½ÈˆèÀ¹…¹¡½É}Á¡É…Í”°(€€€€€€€€€€€€€€€€‰É•…Í½¸ˆèÀ¹É•…Í½¸°(€€€€€€€€€€€ô(€€€€€€€€€€€™½ÈÀ¥¸Á±…•µ•¹ÑÌ(€€€€€€€t°(€€€€€€€€‰…¹¡½É}½Õ¹Ðˆè±•¸¡…¹¡½ÉÌ¤°(€€€€€€€€‰ÍÑ…µÁ•ˆè‰½½°¡Á±…•µ•¹ÑÌ¤°(€€€€€€€€‰¹••‘Í}É•Ù¥•Üˆè¹½Ð‰½½°¡Á±…•µ•¹ÑÌ¤°(€€€ô(()‘•˜µ…¥¸ ¤€´ø9½¹”è(€€€Á…ÉÍ•È€ô…ÉÁ…ÉÍ”¹ÉÕµ•¹ÑA…ÉÍ•È¡‘•ÍÉ¥ÁÑ¥½¸ô‰$‘•Ñ•Éµ¥¹¥ÍÑ¥ŒAÍÑ…µÀ•¹¥¹”ˆ¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µ¥¹ÁÕÐˆ°É•ÅÕ¥É•õQÉÕ”°ÑåÁ”õA…Ñ ¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µÍÑ…µÀˆ°É•ÅÕ¥É•õQÉÕ”°ÑåÁ”õA…Ñ ¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µ½ÕÑÁÕÐˆ°É•ÅÕ¥É•õQÉÕ”°ÑåÁ”õA…Ñ ¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µÍÑ…µÀµÝ¥‘Ñ ˆ°ÑåÁ”õ™±½…Ð°‘•™…Õ±ÐôÄÜÔ¸À¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µ…±±½Üµ™…±±‰…¬ˆ°…Ñ¥½¸ô‰ÍÑ½É•}ÑÉÕ”ˆ¤(€€€…ÉÌ€ôÁ…ÉÍ•È¹Á…ÉÍ•}…ÉÌ ¤((€€€É•ÍÕ±Ð€ôÍÑ…µÁ}Á‘˜¡…ÉÌ¹¥¹ÁÕÐ°…ÉÌ¹ÍÑ…µÀ°…ÉÌ¹½ÕÑÁÕÐ°…ÉÌ¹ÍÑ…µÁ}Ý¥‘Ñ °…ÉÌ¹…±±½Ý}™…±±‰…¬¤(€€€ÁÉ¥¹Ð¡©Í½¸¹‘ÕµÁÌ¡É•ÍÕ±Ð°•¹ÍÕÉ•}…Í¥¤õ…±Í”°¥¹‘•¹ÐôÈ¤¤(()¥˜}}¹…µ•}|€ôô€‰}}µ…¥¹}|ˆè(€€€µ…¥¸ ¤(