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
]

GENERIC_TARGETS = {"TRANSPORTATOR", "CARRIER", "HAULIER", "VETTORE"}
FOOTER_TARGETS = {"DEGEI LOGISTIC", "RO36256981"}
DEDICATED_TARGETS = {
    phrase
    for phrase, _score in TARGET_PHRASES
    if phrase not in FOOTER_TARGETS and phrase not in GENERIC_TARGETS
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


def find_anchors(pdf_path: Path) -> tuple[list[Anchor], list[list[Box]], list[tuple[float, float]]]:
    anchors: list[Anchor] = []
    all_word_boxes: list[list[Box]] = []
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

            lines = group_lines(words)
            for line in lines:
                line_norm = line["norm"]
                for phrase, base_score in TARGET_PHRASES:
                    if contains_phrase(line_norm, phrase):
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
                        anchors.append(
                            Anchor(
                                page_index=page_index,
                                box=anchor_box,
                                score=score,
                                phrase=phrase,
                                line_text=line["text"],
                            )
                        )
    return anchors, all_word_boxes, page_sizes


def looks_like_carrier_footer(box: Box, page_w: float, page_h: float) -> bool:
    return box.top > page_h * 0.68 and box.x0 > page_w * 0.45


def is_footer_anchor(anchor: Anchor, page_w: float, page_h: float) -> bool:
    return anchor.phrase in FOOTER_TARGETS and looks_like_carrier_footer(anchor.box, page_w, page_h)


def anchor_rank(anchor: Anchor, page_w: float, page_h: float) -> tuple[int, int, float]:
    if anchor.phrase in DEDICATED_TARGETS:
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


def stamp_size_for_anchor(anchor: Anchor, page_w: float, page_h: float, requested_w: float, ratio: float) -> tuple[float, float]:
    # Make sends a pixel-like target width. Convert it to a professional PDF size
    # based on the signature zone so it matches client stamps instead of dominating them.
    requested_w = max(70.0, requested_w)
    if is_footer_anchor(anchor, page_w, page_h):
        width = min(requested_w, 112.0, page_w * 0.19)
        max_height = 52.0
        min_width = 62.0
    elif anchor.phrase in FOOTER_TARGETS:
        width = min(requested_w, 112.0, page_w * 0.19)
        max_height = 52.0
        min_width = 62.0
    else:
        width = min(requested_w, 132.0, page_w * 0.22)
        max_height = 62.0
        min_width = 78.0
    if ratio > 0:
        width = min(width, max_height / ratio)
    width = max(min_width, width)
    return width, width * ratio


def placement_candidates(anchor: Anchor, page_w: float, page_h: float, stamp_w: float, stamp_h: float) -> list[tuple[str, Box]]:
    a = anchor.box
    gap = 12
    if is_footer_anchor(anchor, page_w, page_h):
        center_x = (a.x0 + a.x1) / 2
        preferred_x = center_x - (stamp_w / 2)
        preferred_top = a.top - stamp_h - 8
        right_column_x = page_w - stamp_w - 80
        right_of_name_x = a.x1 + 8
        right_of_name_top = a.top - stamp_h - 4
        return [
            ("right_of_footer_name", Box(right_of_name_x, right_of_name_top, right_of_name_x + stamp_w, right_of_name_top + stamp_h)),
            ("above_footer_name", Box(preferred_x, preferred_top, preferred_x + stamp_w, preferred_top + stamp_h)),
            ("above_footer_right", Box(right_column_x, preferred_top, right_column_x + stamp_w, preferred_top + stamp_h)),
            ("footer_column_center", Box(page_w * 0.64, preferred_top, page_w * 0.64 + stamp_w, preferred_top + stamp_h)),
            ("footer_slightly_higher", Box(preferred_x, preferred_top - 18, preferred_x + stamp_w, preferred_top - 18 + stamp_h)),
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
    xs = [page_w - stamp_w - 60, page_w - stamp_w - 130, page_w * 0.52]
    ys = [page_h - stamp_h - 90, page_h - stamp_h - 170, page_h * 0.56, page_h * 0.46]
    out = []
    for yi, y in enumerate(ys):
        for xi, x in enumerate(xs):
            out.append((f"fallback_{yi}_{xi}", Box(x, y, x + stamp_w, y + stamp_h)))
    return out


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


def choose_best_candidate(
    candidates: list[tuple[str, Box]],
    word_boxes: list[Box],
    page_w: float,
    page_h: float,
    anchor: Anchor | None,
) -> tuple[str, Box]:
    scored = score_candidates(candidates, word_boxes, page_w, page_h, anchor)
    safe = [item for item in scored if item[3] <= 0.015]
    pool = safe or scored
    best_reason, best_rect, _score, _text_overlap = max(pool, key=lambda item: item[2])
    return best_reason, best_rect


def score_candidates(
    candidates: list[tuple[str, Box]],
    word_boxes: list[Box],
    page_w: float,
    page_h: float,
    anchor: Anchor | None,
) -> list[tuple[str, Box, float, float]]:
    return [
        (reason, rect, score_rect(rect, word_boxes, page_w, page_h, anchor), overlap_ratio(rect, word_boxes))
        for reason, rect in candidates
    ]


def choose_footer_candidate(
    anchor: Anchor,
    word_boxes: list[Box],
    page_w: float,
    page_h: float,
    stamp_w: float,
    stamp_ratio: float,
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
        scored = score_candidates(candidates, word_boxes, page_w, page_h, anchor)
        safe = [item for item in scored if item[3] <= 0.015]
        if safe:
            reason, rect, _score, _overlap = max(safe, key=lambda item: item[2])
            return reason, rect
        attempt_best = max(scored, key=lambda item: item[2])
        if best_any is None or attempt_best[2] > best_any[2]:
            best_any = attempt_best
    assert best_any is not None
    return best_any[0], best_any[1]


def choose_placements(
    anchors: list[Anchor],
    word_boxes: list[list[Box]],
    page_sizes: list[tuple[float, float]],
    stamp_w: float,
    stamp_ratio: float,
    allow_fallback: bool = False,
) -> list[Placement]:
    placements: list[Placement] = []
    page_count = len(page_sizes)
    high_anchors = [a for a in anchors if a.score >= 95]
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
        page_stamp_w, page_stamp_h = stamp_size_for_anchor(anchor, page_w, page_h, stamp_w, stamp_ratio)
        if is_footer_anchor(anchor, page_w, page_h):
            best_reason, best_rect = choose_footer_candidate(
                anchor,
                word_boxes[page_index],
                page_w,
                page_h,
                page_stamp_w,
                stamp_ratio,
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

    if not placements and page_count and allow_fallback:
        page_index = page_count - 1
        page_w, page_h = page_sizes[page_index]
        page_stamp_w = min(max(stamp_w, 78.0), 112.0, page_w * 0.19)
        page_stamp_h = page_stamp_w * stamp_ratio
        candidates = [
            (reason, clamp_rect(rect, page_w, page_h))
            for reason, rect in fallback_candidates(page_w, page_h, page_stamp_w, page_stamp_h)
        ]
        best_reason, best_rect = choose_best_candidate(
            candidates,
            word_boxes[page_index],
            page_w,
            page_h,
            None,
        )
        placements.append(
            Placement(
                page_index=page_index,
                rect=best_rect,
                score=score_rect(best_rect, word_boxes[page_index], page_w, page_h, None),
                anchor_phrase="FALLBACK_LAST_PAGE",
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

    anchors, word_boxes, page_sizes = find_anchors(input_pdf)
    placements = choose_placements(
        anchors,
        word_boxes,
        page_sizes,
        stamp_width,
        ratio,
        allow_fallback=allow_fallback,
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
