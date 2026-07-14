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


ENGINE_VERSION = "2026-07-14-all-carrier-pages-v14"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("STAMP_OUTPUT_DIR", tempfile.gettempdir())) / "degei_stamp_engine"
API_KEY = os.environ.get("STAMP_API_KEY", "")
MAX_DOWNLOAD_BYTES = int(os.environ.get("STAMP_MAX_DOWNLOAD_BYTES", str(35 * 1024 * 1024)))
FILE_TTL_SECONDS = int(os.environ.get("STAMP_FILE_TTL_SECONDS", str(2 * 60 * 60)))


def patch_stamp_engine() -> None:
    original_choose_placements = _stamp_engine.choose_placements

    def looks_like_carrier_signature_block(box, page_w, page_h):
        in_lower_page = page_h * 0.52 < box.top < page_h * 0.90
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
            if same_side and near_column and anchor.box.top - 30 <= box.top <= anchor.box.top + 55:
                bottom = max(bottom, box.bottom)
        return bottom

    def select_transporter_signature_anchors(anchors, word_boxes, image_boxes, page_sizes):
        signature_anchors = [
            anchor
            for anchor in anchors
            if anchor.phrase in _stamp_engine.TRANSPORTER_NAME_TARGETS
            and _stamp_engine.is_signature_block_anchor(
                anchor,
                *page_sizes[anchor.page_index],
            )
        ]
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
                preferred_top = max(preferred_top, ref_centered_top, ref.top + 3)
            aligned_x = min(max(anchor.box.x0, 28), page_w - width - 28)
            candidates = [
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
            if ref is not None:
                aligned_top = max(text_bottom + 3, ref.top + (ref.height - height) / 2)
                candidates.append(
                    (
                        "signature_block_align_client_stamp",
                        _stamp_engine.Box(
                            aligned_x,
                            aligned_top,
                            aligned_x + width,
                            aligned_top + height,
                        ),
                    )
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
        ref = (
            _stamp_engine.reference_stamp_box(anchor, image_boxes or [])
            if _stamp_engine.is_signature_block_anchor(anchor, page_w, page_h)
            else None
        )
        if ref is not None:
            target_height = min(max(ref.height * 0.92, 58.0), 82.0, page_h * 0.11)
            width = min(
                max(target_height / max(ratio, 0.1), ref.width * 0.92, 92.0),
                165.0,
                page_w * 0.28,
            )
            max_height = 82.0
            min_width = min(92.0, page_w * 0.20)
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
        right_margins = [55, 85, 120, 160, 210]
        bottom_margins = [65, 95, 130, 170, 215, 265]
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
                reason, rect, _score, _overlap = max(safe, key=lambda item: item[2])
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
        selected_signature_anchors = select_transporter_signature_anchors(
            anchors,
            word_boxes,
            image_boxes,
            page_sizes,
        )
        placement_anchors = selected_signature_anchors or anchors
        placements = original_choose_placements(
            placement_anchors,
            word_boxes,
            image_boxes,
            page_sizes,
            stamp_w,
            stamp_ratio,
            allow_fallback=False,
            visual_pages=visual_pages,
        )
        page_count = len(page_sizes)
        if placements or not page_count or not allow_fallback:
            return placements

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
                    score=_stamp_engine.score_rect(best_rect, word_boxes[page_index], page_w, page_h, None),
                    anchor_phrase="FALLBACK_EACH_PAGE",
                    reason=best_reason,
                )
            )
        return placements

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

