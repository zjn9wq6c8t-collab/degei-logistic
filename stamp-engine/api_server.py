from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import stamp_engine as _stamp_engine


ENGINE_VERSION = "2026-08-14-weak-anchor-fallback-all-pages-v23"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("STAMP_OUTPUT_DIR", tempfile.gettempdir())) / "degei_stamp_engine"
API_KEY = os.environ.get("STAMP_API_KEY", "")
MAX_DOWNLOAD_BYTES = int(os.environ.get("STAMP_MAX_DOWNLOAD_BYTES", str(35 * 1024 * 1024)))
FILE_TTL_SECONDS = int(os.environ.get("STAMP_FILE_TTL_SECONDS", str(2 * 60 * 60)))


def patch_stamp_engine() -> None:
    original_choose_placements = _stamp_engine.choose_placements

    def looks_like_carrier_signature_block(box, page_w, page_h):
        # Some order templates place the carrier confirmation footer almost
        # against the physical bottom edge. Keep these anchors eligible so a
        # repeated footer is stamped on every matching page, not only on pages
        # where the footer happens to sit a few points higher.
        in_lower_page = page_h * 0.52 < box.top < page_h * 0.975
        in_side_column = box.x0 < page_w * 0.42 or box.x1 > page_w * 0.58
        return in_lower_page and in_side_column

    def signature_block_text_bottom(anchor, word_boxes, page_w):
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
            in_signature_lines = (
                anchor.box.top - 10
                <= box.top
                <= anchor.box.bottom + 34
            )
            if same_side and near_column and in_signature_lines:
                bottom = max(bottom, box.bottom)
        return bottom

    def is_split_transporter_name_pair(anchor, anchors, page_w, page_h):
        """Recognize a carrier signature block split over nearby PDF lines."""
        if anchor.phrase not in _stamp_engine.TRANSPORTER_NAME_TARGETS:
            return False

        anchor_center = (anchor.box.x0 + anchor.box.x1) / 2
        anchor_on_right = anchor_center >= page_w * 0.50
        line_norm = _stamp_engine.norm(anchor.line_text)
        same_line_pair = (
            "TRANSPORTATOR" in line_norm
            and (
                "DEGEI LOGISTIC" in line_norm
                or "RO36256981" in line_norm
            )
        )

        for label in anchors:
            if label.page_index != anchor.page_index:
                continue
            if (
                label.phrase not in _stamp_engine.GENERIC_TARGETS
                and label.phrase not in _stamp_engine.CONFIRMATION_HEADING_TARGETS
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
            block_top = min(label.box.top, anchor.box.top)
            if (
                same_side
                and horizontally_related
                and vertical_gap <= max(52.0, page_h * 0.085)
                and block_top >= page_h * 0.38
            ):
                return True

        # Avoid interpreting the "Transportator: Nume firma" header near the
        # top of page one as a signature block.
        return (
            same_line_pair
            and anchor.box.top >= page_h * 0.52
            and (anchor.box.x0 >= page_w * 0.48 or anchor.box.x1 <= page_w * 0.52)
        )

    def is_carrier_identity_header(anchor, anchors, page_w, page_h):
        """Reject carrier company-data rows that are not signature fields."""
        if anchor.phrase not in _stamp_engine.TRANSPORTER_NAME_TARGETS:
            return False

        line_norm = _stamp_engine.norm(anchor.line_text)
        if "TRANSPORTATOR" in line_norm and any(
            marker in line_norm
            for marker in ("NUME FIRMA", "DENUMIRE FIRMA", "DATE FIRMA", "DATE TRANSPORTATOR")
        ):
            return True

        if anchor.box.top >= page_h * 0.62:
            return False

        for tax_anchor in anchors:
            if (
                tax_anchor.page_index != anchor.page_index
                or tax_anchor.phrase != "RO36256981"
            ):
                continue
            vertical_gap = tax_anchor.box.top - anchor.box.bottom
            horizontally_related = (
                tax_anchor.box.x1 >= anchor.box.x0 - page_w * 0.12
                and tax_anchor.box.x0 <= anchor.box.x1 + page_w * 0.12
            )
            if 0 <= vertical_gap <= max(34.0, page_h * 0.055) and horizontally_related:
                return True
        return False

    def select_transporter_signature_anchors(anchors, word_boxes, image_boxes, page_sizes):
        signature_anchors = []
        for anchor in anchors:
            if anchor.phrase not in _stamp_engine.TRANSPORTER_NAME_TARGETS:
                continue
            page_w, page_h = page_sizes[anchor.page_index]
            if is_carrier_identity_header(anchor, anchors, page_w, page_h):
                continue
            if _stamp_engine.is_signature_block_anchor(
                anchor,
                page_w,
                page_h,
            ) or _stamp_engine.is_paired_signature_anchor(
                anchor,
                image_boxes[anchor.page_index],
                page_w,
                page_h,
            ) or is_split_transporter_name_pair(
                anchor,
                anchors,
                page_w,
                page_h,
            ):
                signature_anchors.append(anchor)
        page_indexes = {anchor.page_index for anchor in signature_anchors}
        if not page_indexes:
            return None

        anchors_by_page = {}
        for anchor in signature_anchors:
            anchors_by_page.setdefault(anchor.page_index, []).append(anchor)

        return [
            max(
                anchors_by_page[page_index],
                key=lambda item: _stamp_engine.anchor_rank(
                    item,
                    *page_sizes[page_index],
                ),
            )
            for page_index in sorted(page_indexes)
        ]

    def choose_tight_labeled_candidate(
        anchor,
        anchors,
        word_boxes,
        page_w,
        page_h,
        stamp_w,
        stamp_h,
    ):
        """Place a stamp at a certain carrier label when blank space is tight."""
        ratio = stamp_h / max(stamp_w, 1.0)
        text_bottom = signature_block_text_bottom(anchor, word_boxes, page_w)
        anchor_center = (anchor.box.x0 + anchor.box.x1) / 2
        on_right = anchor_center >= page_w * 0.50

        nearby_anchor_boxes = [
            item.box
            for item in anchors
            if item.page_index == anchor.page_index
            and (
                item.phrase in _stamp_engine.GENERIC_TARGETS
                or item.phrase in _stamp_engine.TRANSPORTER_NAME_TARGETS
                or item.phrase in _stamp_engine.SIGNATURE_LABEL_TARGETS
                or item.phrase in _stamp_engine.CONFIRMATION_HEADING_TARGETS
            )
            and ((item.box.x0 + item.box.x1) / 2 >= page_w * 0.50) == on_right
            and item.box.top <= text_bottom + 12
            and item.box.bottom >= anchor.box.top - 70
        ]
        block_top = min(
            [anchor.box.top, *[box.top for box in nearby_anchor_boxes]]
        )
        allowed_boxes = [
            box
            for box in word_boxes
            if ((box.x0 + box.x1) / 2 >= page_w * 0.50) == on_right
            and box.top >= block_top - 10
            and box.bottom <= text_bottom + 12
        ]
        unrelated_boxes = [box for box in word_boxes if box not in allowed_boxes]

        widths = []
        for width in (
            stamp_w,
            min(stamp_w, 108.0),
            stamp_w * 0.92,
            stamp_w * 0.84,
            stamp_w * 0.76,
            78.0,
            70.0,
        ):
            width = min(max(width, 70.0), page_w * 0.22)
            if all(abs(width - existing) > 0.5 for existing in widths):
                widths.append(width)

        candidates = []
        for width in widths:
            height = width * ratio
            if on_right:
                x_options = (
                    min(max(anchor.box.x0, page_w * 0.54), page_w - width - 18),
                    page_w - width - 22,
                    min(max(anchor_center - width / 2, page_w * 0.52), page_w - width - 18),
                )
            else:
                x_options = (
                    max(18.0, min(anchor.box.x0, page_w * 0.48 - width)),
                    22.0,
                    max(18.0, anchor_center - width / 2),
                )

            ideal_top = text_bottom + 2.0
            latest_top = page_h - height - 12.0
            top_options = (
                ideal_top,
                latest_top,
                max(block_top, text_bottom - height * 0.18),
                max(block_top - 2.0, latest_top),
            )
            for x in x_options:
                for top in top_options:
                    rect = _stamp_engine.clamp_rect(
                        _stamp_engine.Box(x, top, x + width, top + height),
                        page_w,
                        page_h,
                        margin=12,
                    )
                    unrelated_overlap = _stamp_engine.overlap_ratio(
                        rect,
                        unrelated_boxes,
                    )
                    carrier_overlap = _stamp_engine.overlap_ratio(
                        rect,
                        allowed_boxes,
                    )
                    below_distance = abs(rect.top - ideal_top)
                    shrink_penalty = max(0.0, stamp_w - width)
                    cost = (
                        unrelated_overlap * 120000.0
                        + carrier_overlap * 600.0
                        + below_distance * 1.6
                        + shrink_penalty * 0.8
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

    def choose_signature_block_candidate(
        anchor,
        word_boxes,
        image_boxes,
        page_w,
        page_h,
        stamp_w,
        stamp_h,
        page_image=None,
    ):
        ref = _stamp_engine.reference_stamp_box(anchor, image_boxes)
        text_bottom = signature_block_text_bottom(anchor, word_boxes, page_w)
        best_any = None
        ratio = stamp_h / max(stamp_w, 1.0)
        for scale in (1.0, 0.92, 0.84, 0.76, 0.68, 0.60, 0.52):
            width = max(58.0, stamp_w * scale)
            height = width * ratio
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
                            _stamp_engine.Box(
                                aligned_x,
                                aligned_top,
                                aligned_x + width,
                                aligned_top + height,
                            ),
                        ),
                        (
                            "below_signature_block_match_client",
                            _stamp_engine.Box(x, aligned_top, x + width, aligned_top + height),
                        ),
                        (
                            "signature_block_client_higher",
                            _stamp_engine.Box(
                                aligned_x,
                                max(text_bottom + 3, aligned_top - 10),
                                aligned_x + width,
                                max(text_bottom + 3, aligned_top - 10) + height,
                            ),
                        ),
                        (
                            "signature_block_client_lower",
                            _stamp_engine.Box(
                                aligned_x,
                                aligned_top + 10,
                                aligned_x + width,
                                aligned_top + 10 + height,
                            ),
                        ),
                    ]
                )
            else:
                candidates.extend(
                    [
                        (
                            "below_signature_block_match_client",
                            _stamp_engine.Box(x, preferred_top, x + width, preferred_top + height),
                        ),
                        (
                            "below_signature_block_aligned",
                            _stamp_engine.Box(aligned_x, preferred_top, aligned_x + width, preferred_top + height),
                        ),
                        (
                            "signature_block_lower",
                            _stamp_engine.Box(x, preferred_top + 18, x + width, preferred_top + 18 + height),
                        ),
                    ]
                )
            candidates = [
                (reason, _stamp_engine.clamp_rect(rect, page_w, page_h))
                for reason, rect in candidates
            ]
            scored = _stamp_engine.score_candidates(
                candidates,
                word_boxes,
                page_w,
                page_h,
                anchor,
                page_image,
            )
            safe = [
                item
                for item in scored
                if _stamp_engine.is_safe_rect(
                    item[1],
                    word_boxes,
                    page_image,
                    page_w,
                    page_h,
                )
                and item[1].top >= text_bottom + 2
            ]
            if safe:
                reason, rect, _score, _overlap = max(safe, key=lambda item: item[2])
                return reason, rect
            attempt_best = max(scored, key=lambda item: item[2])
            if best_any is None or attempt_best[2] > best_any[2]:
                best_any = attempt_best
        return best_any[0], best_any[1]

    # Keep the visual placement logic in stamp_engine.py, but let the API tune
    # final size quickly when Make sends stamp_width as a broad target.
    def stamp_size_for_anchor(anchor, page_w, page_h, requested_w, ratio, image_boxes=None):
        requested_w = max(70.0, requested_w)
        ref = None
        if (
            anchor.phrase in _stamp_engine.TRANSPORTER_NAME_TARGETS
            or _stamp_engine.is_signature_block_anchor(anchor, page_w, page_h)
        ):
            ref = _stamp_engine.reference_stamp_box(anchor, image_boxes or [])
        if ref is not None:
            target_height = min(max(ref.height * 0.42, 38.0), 54.0, page_h * 0.075)
            lower_width = max(88.0, requested_w * 0.88)
            upper_width = min(125.0, requested_w * 1.08, page_w * 0.22)
            width = min(max(target_height / max(ratio, 0.1), lower_width), upper_width)
            max_height = 56.0
            min_width = min(lower_width, page_w * 0.20)
        elif anchor.phrase in _stamp_engine.SUPPLIER_SIGNATURE_TARGETS:
            width = min(requested_w, 118.0, page_w * 0.20)
            max_height = 58.0
            min_width = 82.0
        elif _stamp_engine.is_footer_anchor(anchor, page_w, page_h):
            width = min(requested_w, 96.0, page_w * 0.16)
            max_height = 46.0
            min_width = 56.0
        elif anchor.phrase in _stamp_engine.FOOTER_TARGETS:
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

    def fallback_candidates(page_w, page_h, stamp_w, stamp_h):
        right_margins = [28, 42, 60, 85, 120, 160, 210]
        bottom_margins = [32, 48, 68, 92, 122, 160, 205, 255]
        out = []
        for yi, bottom_margin in enumerate(bottom_margins):
            y = page_h - stamp_h - bottom_margin
            for xi, right_margin in enumerate(right_margins):
                x = page_w - stamp_w - right_margin
                out.append(
                    (
                        f"fallback_bottom_right_{yi}_{xi}",
                        _stamp_engine.Box(x, y, x + stamp_w, y + stamp_h),
                    )
                )
        return out

    def choose_fallback_page_candidate(word_boxes, page_w, page_h, stamp_w, stamp_ratio, page_image=None):
        attempts = [stamp_w, stamp_w * 0.90, stamp_w * 0.80, stamp_w * 0.70, stamp_w * 0.60]
        best_any = None
        for width in attempts:
            width = max(52.0, width)
            height = width * stamp_ratio
            candidates = [
                (reason, _stamp_engine.clamp_rect(rect, page_w, page_h))
                for reason, rect in fallback_candidates(page_w, page_h, width, height)
            ]
            scored = _stamp_engine.score_candidates(candidates, word_boxes, page_w, page_h, None, page_image)
            safe = [
                item
                for item in scored
                if _stamp_engine.is_safe_rect(item[1], word_boxes, page_image, page_w, page_h)
            ]
            if safe:
                reason, rect, _score, _overlap = max(
                    safe,
                    key=lambda item: (
                        item[2]
                        - (page_h - item[1].bottom) * 0.45
                        - (page_w - item[1].x1) * 0.25
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
        anchors,
        word_boxes,
        image_boxes,
        page_sizes,
        stamp_w,
        stamp_ratio,
        allow_fallback=False,
        visual_pages=None,
    ):
        explicit_signature_anchors = _stamp_engine.select_explicit_signature_anchors(
            anchors,
            word_boxes,
            page_sizes,
        )
        if explicit_signature_anchors:
            explicit_placements = []
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
                best_reason, best_rect = _stamp_engine.choose_explicit_signature_candidate(
                    anchor,
                    word_boxes[page_index],
                    page_w,
                    page_h,
                    page_stamp_w,
                    page_stamp_h,
                    page_image,
                )
                if (
                    anchor.phrase not in _stamp_engine.OVERLAP_OK_SIGNATURE_TARGETS
                    and not _stamp_engine.is_safe_rect(
                        best_rect,
                        word_boxes[page_index],
                        page_image,
                        page_w,
                        page_h,
                    )
                ):
                    best_reason, best_rect = choose_tight_labeled_candidate(
                        anchor,
                        anchors,
                        word_boxes[page_index],
                        page_w,
                        page_h,
                        page_stamp_w,
                        page_stamp_h,
                    )
                explicit_placements.append(
                    _stamp_engine.Placement(
                        page_index=page_index,
                        rect=best_rect,
                        score=_stamp_engine.score_rect(
                            best_rect,
                            word_boxes[page_index],
                            page_w,
                            page_h,
                            anchor,
                        ),
                        anchor_phrase=anchor.phrase,
                        reason=best_reason,
                    )
                )
            return explicit_placements

        selected_signature_anchors = select_transporter_signature_anchors(
            anchors,
            word_boxes,
            image_boxes,
            page_sizes,
        )
        if selected_signature_anchors:
            signature_placements = []
            for anchor in selected_signature_anchors:
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
                if not _stamp_engine.is_safe_rect(
                    best_rect,
                    word_boxes[page_index],
                    page_image,
                    page_w,
                    page_h,
                ):
                    best_reason, best_rect = choose_tight_labeled_candidate(
                        anchor,
                        anchors,
                        word_boxes[page_index],
                        page_w,
                        page_h,
                        page_stamp_w,
                        page_stamp_h,
                    )
                signature_placements.append(
                    _stamp_engine.Placement(
                        page_index=page_index,
                        rect=best_rect,
                        score=_stamp_engine.score_rect(
                            best_rect,
                            word_boxes[page_index],
                            page_w,
                            page_h,
                            anchor,
                        ),
                        anchor_phrase=anchor.phrase,
                        reason=best_reason,
                    )
                )
            return signature_placements

        page_count = len(page_sizes)
        if page_count and allow_fallback:
            # No verified carrier signing block exists. Do not let weak
            # identity/header anchors choose one arbitrary page; the safe
            # production fallback is one bottom-right stamp on every page.
            placements = []
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
                    _stamp_engine.Placement(
                        page_index=page_index,
                        rect=best_rect,
                        score=_stamp_engine.score_rect(
                            best_rect,
                            word_boxes[page_index],
                            page_w,
                            page_h,
                            None,
                        ),
                        anchor_phrase="FALLBACK_EACH_PAGE",
                        reason=best_reason,
                    )
                )
            return placements

        return original_choose_placements(
            anchors,
            word_boxes,
            image_boxes,
            page_sizes,
            stamp_w,
            stamp_ratio,
            allow_fallback=False,
            visual_pages=visual_pages,
        )

    def stamp_pdf(input_pdf, stamp_image, output_pdf, stamp_width=175.0, allow_fallback=False):
        with _stamp_engine.Image.open(stamp_image) as img:
            ratio = img.height / max(img.width, 1)

        anchors, word_boxes, image_boxes, page_sizes = _stamp_engine.find_anchors(input_pdf)
        visual_page_indexes = {anchor.page_index for anchor in anchors}
        if allow_fallback and page_sizes:
            visual_page_indexes.update(range(len(page_sizes)))
        visual_pages = _stamp_engine.render_visual_pages(input_pdf, visual_page_indexes)
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

        reader = _stamp_engine.PdfReader(str(input_pdf))
        writer = _stamp_engine.PdfWriter()
        stamp_reader = _stamp_engine.ImageReader(str(stamp_image))

        placement_by_page = {p.page_index: p for p in placements}
        for page_index, page in enumerate(reader.pages):
            if page_index not in placement_by_page:
                writer.add_page(page)
                continue

            page_w = float(page.mediabox.width)
            page_h = float(page.mediabox.height)
            p = placement_by_page[page_index]
            packet = _stamp_engine.io.BytesIO()
            c = _stamp_engine.canvas.Canvas(packet, pagesize=(page_w, page_h))
            x = p.rect.x0
            y = page_h - p.rect.bottom
            c.drawImage(stamp_reader, x, y, width=p.rect.width, height=p.rect.height, mask="auto")
            c.save()
            packet.seek(0)
            overlay = _stamp_engine.PdfReader(packet).pages[0]
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

    _stamp_engine.stamp_size_for_anchor = stamp_size_for_anchor
    _stamp_engine.fallback_candidates = fallback_candidates
    _stamp_engine.looks_like_carrier_signature_block = looks_like_carrier_signature_block
    _stamp_engine.signature_block_text_bottom = signature_block_text_bottom
    _stamp_engine.is_split_transporter_name_pair = is_split_transporter_name_pair
    _stamp_engine.is_carrier_identity_header = is_carrier_identity_header
    _stamp_engine.choose_tight_labeled_candidate = choose_tight_labeled_candidate
    _stamp_engine.choose_signature_block_candidate = choose_signature_block_candidate
    _stamp_engine.select_transporter_signature_anchors = select_transporter_signature_anchors
    _stamp_engine.choose_placements = choose_placements
    _stamp_engine.stamp_pdf = stamp_pdf


patch_stamp_engine()
stamp_pdf = _stamp_engine.stamp_pdf


def json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def clean_filename(name: str, fallback: str = "comanda_stampilata.pdf") -> str:
    name = Path(name or fallback).name.strip()
    if not name.lower().endswith(".pdf"):
        name = f"{Path(name).stem or 'comanda_stampilata'}.pdf"
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "._- ")
    return safe or fallback


def cleanup_old_files() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - FILE_TTL_SECONDS
    for path in OUTPUT_DIR.glob("*.pdf"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def assert_download_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL invalid. Foloseste doar link http/https.")


def download_file(url: str, destination: Path) -> None:
    assert_download_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "DEGEI-Stamp-Engine/1.0"})
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as f:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError("Fisier prea mare pentru stampilare automata.")
                    f.write(chunk)
    except urllib.error.URLError as exc:
        raise ValueError(f"Nu pot descarca fisierul: {exc}") from exc


class StampHandler(BaseHTTPRequestHandler):
    server_version = "DEGEIStampEngine/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def write_json(self, status: int, payload: dict) -> None:
        data = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self) -> bool:
        if not API_KEY:
            return True
        return self.headers.get("X-API-Key") == API_KEY

    def read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length invalid.") from exc
        if length <= 0 or length > 256 * 1024:
            raise ValueError("Body invalid sau prea mare.")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("JSON invalid.") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON-ul trebuie sa fie obiect.")
        return payload

    def public_base_url(self) -> str:
        configured = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if configured:
            return configured
        proto = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip() or "http"
        host = self.headers.get("Host", "").strip()
        return f"{proto}://{host}".rstrip("/")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self.write_json(
                HTTPStatus.OK,
                {"ok": True, "service": "degei-stamp-engine", "version": ENGINE_VERSION},
            )
            return

        if parsed.path.startswith("/files/"):
            cleanup_old_files()
            name = Path(urllib.parse.unquote(parsed.path.removeprefix("/files/"))).name
            path = OUTPUT_DIR / name
            if not name.endswith(".pdf") or not path.exists():
                self.write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Fisierul nu exista sau a expirat."})
                return
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Ruta inexistenta."})

    def do_POST(self) -> None:
        if urllib.parse.urlparse(self.path).path != "/stamp":
            self.write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Ruta inexistenta."})
            return
        if not self.authorized():
            self.write_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Cheie API lipsa sau gresita."})
            return

        cleanup_old_files()
        work_dir = Path(tempfile.mkdtemp(prefix="degei_stamp_"))
        try:
            payload = self.read_json_body()
            pdf_url = str(payload.get("pdf_url", "")).strip()
            stamp_url = str(payload.get("stamp_url", "")).strip()
            if not pdf_url or not stamp_url:
                raise ValueError("pdf_url si stamp_url sunt obligatorii.")

            stamp_width = float(payload.get("stamp_width", 175.0))
            # Fallback is now a required safety behavior: if no clear carrier
            # confirmation zone is found, stamp every page in the safest
            # bottom-right free area instead of returning needs_review.
            allow_fallback = True
            filename = clean_filename(str(payload.get("filename", "comanda_stampilata.pdf")))

            input_pdf = work_dir / "input.pdf"
            stamp_image = work_dir / "stamp.png"
            output_pdf = work_dir / filename
            download_file(pdf_url, input_pdf)
            download_file(stamp_url, stamp_image)

            result = stamp_pdf(
                input_pdf=input_pdf,
                stamp_image=stamp_image,
                output_pdf=output_pdf,
                stamp_width=stamp_width,
                allow_fallback=allow_fallback,
            )

            if result["needs_review"]:
                self.write_json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {
                        "ok": False,
                        "version": ENGINE_VERSION,
                        "needs_review": True,
                        "error": "Nu am gasit o zona clara de semnatura/stampila transportator. Nu trimit PDF stampilat la ghici.",
                        "anchor_count": result["anchor_count"],
                    },
                )
                return

            token = secrets.token_urlsafe(18)
            public_name = f"{Path(filename).stem}-{token}.pdf"
            public_path = OUTPUT_DIR / public_name
            shutil.copyfile(output_pdf, public_path)
            file_url = f"{self.public_base_url()}/files/{urllib.parse.quote(public_name)}"

            self.write_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "version": ENGINE_VERSION,
                    "file_url": file_url,
                    "filename": filename,
                    "placements": result["placements"],
                    "anchor_count": result["anchor_count"],
                    "needs_review": False,
                },
            )
        except ValueError as exc:
            self.write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "version": ENGINE_VERSION, "error": str(exc)})
        except Exception as exc:
            self.write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "version": ENGINE_VERSION, "error": f"Eroare stampilare: {exc}"},
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", port), StampHandler)
    print(f"DEGEI Stamp Engine {ENGINE_VERSION} listening on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
