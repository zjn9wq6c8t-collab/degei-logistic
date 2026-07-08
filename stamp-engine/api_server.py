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


ENGINE_VERSION = "2026-07-08-visual-safety-v10"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("STAMP_OUTPUT_DIR", tempfile.gettempdir())) / "degei_stamp_engine"
API_KEY = os.environ.get("STAMP_API_KEY", "")
MAX_DOWNLOAD_BYTES = int(os.environ.get("STAMP_MAX_DOWNLOAD_BYTES", str(35 * 1024 * 1024)))
FILE_TTL_SECONDS = int(os.environ.get("STAMP_FILE_TTL_SECONDS", str(2 * 60 * 60)))


def patch_stamp_engine() -> None:
    # All production placement logic lives in stamp_engine.py. Older deployments
    # patched several helpers here; keeping this hook as a no-op avoids stale
    # overrides shadowing the visual safety checks.
    return

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
