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


ENGINE_VERSION = "2026-07-08-signature-block-v9"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("STAMP_OUTPUT_DIR", tempfile.gettempdir())) / "degei_stamp_engine"
API_KEY = os.environ.get("STAMP_API_KEY", "")
MAX_DOWNLOAD_BYTES = int(os.environ.get("STAMP_MAX_DOWNLOAD_BYTES", str(35 * 1024 * 1024)))
FILE_TTL_SECONDS = int(os.environ.get("STAMP_FILE_TTL_SECONDS", str(2 * 60 * 60)))


def patch_stamp_engine() -> None:
    def looks_like_carrier_footer(box, page_w: float, page_h: float) -> bool:
        return box.top > page_h * 0.78 and box.x0 > page_w * 0.45

    def looks_like_carrier_signature_block(box, page_w: float, page_h: float) -> bool:
        return page_h * 0.14 < box.top < page_h * 0.78 and box.x0 > page_w * 0.48

    def stamp_size_for_anchor(anchor, page_w: float, page_h: float, requested_w: float, ratio: float, image_boxes=None):
        requested_w = max(70.0, requested_w)
        ref = (
            _stamp_engine.reference_stamp_box(anchor, image_boxes or [])
            if _stamp_engine.is_signature_block_anchor(anchor, page_w, page_h)
            else None
        )
        if ref is not None:
            target_height = min(max(ref.height * 1.08, 70.0), 94.0, page_h * 0.13)
            width = min(
                max(target_height / max(ratio, 0.1), ref.width * 1.08, 108.0),
                188.0,
                page_w * 0.32,
            )
            max_height = 94.0
            min_width = min(108.0, page_w * 0.24)
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

    def choose_signature_block_candidate(anchor, word_boxes, image_boxes, page_w, page_h, stamp_w, stamp_h):
        ref = _stamp_engine.reference_stamp_box(anchor, image_boxes)
        text_bottom = _stamp_engine.signature_block_text_bottom(anchor, word_boxes, page_w)
        best_any = None
        for scale in (1.0, 0.92, 0.84, 0.76):
            width = max(82.0, stamp_w * scale)
            height = width * (stamp_h / max(stamp_w, 1.0))
            center_x = min(max((anchor.box.x0 + anchor.box.x1) / 2, page_w * 0.64), page_w - width / 2 - 35)
            x = center_x - width / 2
            preferred_top = text_bottom + 8
            if ref is not None:
                ref_centered_top = ref.top + (ref.height - height) / 2
                preferred_top = max(preferred_top, ref_centered_top, ref.top + 6)
            right_x = min(max(anchor.box.x0, 28), page_w - width - 28)
            candidates = [
                ("below_signature_block_match_client", _stamp_engine.Box(x, preferred_top, x + width, preferred_top + height)),
                ("below_signature_block_right", _stamp_engine.Box(right_x, preferred_top, right_x + width, preferred_top + height)),
                ("signature_block_lower", _stamp_engine.Box(x, preferred_top + 18, x + width, preferred_top + 18 + height)),
            ]
            if ref is not None:
                aligned_top = max(text_bottom + 8, ref.top + (ref.height - height) / 2)
                candidates.append(
                    ("signature_block_align_client_stamp", _stamp_engine.Box(right_x, aligned_top, right_x + width, aligned_top + height))
                )
            safe_candidates = [
                (reason, _stamp_engine.clamp_rect(rect, page_w, page_h))
                for reason, rect in candidates
            ]
            scored = _stamp_engine.score_candidates(safe_candidates, word_boxes, page_w, page_h, anchor)
            safe = [item for item in scored if item[3] <= 0.012]
            if safe:
                reason, rect, _score, _overlap = max(safe, key=lambda item: item[2])
                return reason, rect
            attempt_best = max(scored, key=lambda item: item[2])
            if best_any is None or attempt_best[2] > best_any[2]:
                best_any = attempt_best
        assert best_any is not None
        return best_any[0], best_any[1]

    def score_candidates(candidates, word_boxes, page_w, page_h, anchor):
        reason_bonus = {
            "below_signature_block_match_client": 220.0,
            "signature_block_align_client_stamp": 210.0,
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
                _stamp_engine.score_rect(rect, word_boxes, page_w, page_h, anchor) + reason_bonus.get(reason, 0.0),
                _stamp_engine.overlap_ratio(rect, word_boxes),
            )
            for reason, rect in candidates
        ]

    _stamp_engine.looks_like_carrier_footer = looks_like_carrier_footer
    _stamp_engine.looks_like_carrier_signature_block = looks_like_carrier_signature_block
    _stamp_engine.stamp_size_for_anchor = stamp_size_for_anchor
    _stamp_engine.choose_signature_block_candidate = choose_signature_block_candidate
    _stamp_engine.score_candidates = score_candidates


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
            allow_fallback = bool(payload.get("allow_fallback", False))
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
