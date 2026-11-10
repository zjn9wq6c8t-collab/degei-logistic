from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

import stamp_engine as engine


class VisualFallbackTests(unittest.TestCase):
    def make_stamp(self, root):
        stamp = root / "stamp.png"
        Image.new("RGBA", (150, 100), (0, 75, 150, 255)).save(stamp)
        return stamp

    def make_blank_pdf(self, root, page_count):
        source = root / "blank.pdf"
        writer = PdfWriter()
        for _ in range(page_count):
            writer.add_blank_page(width=595, height=842)
        with source.open("wb") as handle:
            writer.write(handle)
        return source

    def test_missing_visual_evidence_is_not_a_blank_page(self):
        self.assertIsNone(
            engine.choose_fallback_page_candidate([], 595, 842, 102, .68, None)
        )

    def test_render_failure_on_image_only_pdf_requires_review(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scan = root / "scan.png"
            Image.new("RGB", (595, 842), "black").save(scan)
            source = root / "scanned.pdf"
            pdf = canvas.Canvas(str(source), pagesize=(595, 842))
            pdf.drawImage(str(scan), 0, 0, width=595, height=842)
            pdf.save()
            output = root / "result.pdf"
            with (
                patch.object(engine, "tesseract_command", return_value="unused-tesseract"),
                patch.object(engine, "convert_from_path", side_effect=RuntimeError("render failed")),
            ):
                result = engine.stamp_pdf(source, self.make_stamp(root), output, allow_fallback=True)
            self.assertTrue(result["needs_review"])
            self.assertFalse(result["stamped"])
            self.assertEqual(result["placements"], [])
            self.assertEqual(len(PdfReader(output).pages[0].images), 1)

    def test_partial_render_failure_cannot_create_partial_confirmation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_blank_pdf(root, 2)
            output = root / "result.pdf"
            with (
                patch.object(engine, "ocr_page_words", return_value=[]),
                patch.object(
                    engine, "render_visual_pages",
                    return_value={0: Image.new("RGB", (595, 842), "white")},
                ),
            ):
                result = engine.stamp_pdf(source, self.make_stamp(root), output, allow_fallback=True)
            self.assertTrue(result["needs_review"])
            self.assertFalse(result["stamped"])
            self.assertEqual(result["placements"], [])
            self.assertEqual([len(page.images) for page in PdfReader(output).pages], [0, 0])

    def test_rendered_blank_pages_still_allow_fallback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_blank_pdf(root, 2)
            output = root / "result.pdf"
            with (
                patch.object(engine, "ocr_page_words", return_value=[]),
                patch.object(
                    engine, "render_visual_pages",
                    return_value={i: Image.new("RGB", (595, 842), "white") for i in range(2)},
                ),
            ):
                result = engine.stamp_pdf(source, self.make_stamp(root), output, allow_fallback=True)
            self.assertFalse(result["needs_review"])
            self.assertTrue(result["stamped"])
            self.assertEqual([p["page"] for p in result["placements"]], [1, 2])
            self.assertEqual([len(page.images) for page in PdfReader(output).pages], [1, 1])


if __name__ == "__main__":
    unittest.main()
