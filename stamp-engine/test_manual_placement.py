from pathlib import Path
from tempfile import TemporaryDirectory
import shutil
import os
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
import stamp_engine


class ManualPlacementTests(unittest.TestCase):
    def source(self, root, rotation=0):
        raw = root / "raw.pdf"
        c = canvas.Canvas(str(raw), pagesize=(595, 842))
        for number in range(1, 5):
            c.drawString(50, 790, f"TEST AUTOMATIZARE - pagina {number}")
            c.drawString(340, 245, "Transportator DEGEI LOGISTIC")
            c.showPage()
        c.save()
        source = root / "source.pdf"
        writer = PdfWriter()
        for page in PdfReader(raw).pages:
            if rotation:
                page.rotate(rotation)
            writer.add_page(page)
        with source.open("wb") as output:
            writer.write(output)
        stamp = root / "stamp.png"
        image = Image.new("RGBA", (300, 120), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((1, 1, 298, 118), outline=(0, 75, 150), width=6)
        draw.text((50, 50), "DEGEI TEST", fill=(0, 75, 150))
        image.save(stamp)
        return source, stamp

    def test_only_selected_pages_and_exact_coordinates_without_ocr_or_rotation(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation), TemporaryDirectory() as folder:
                root = Path(folder)
                source, stamp = self.source(root, rotation)
                output = root / "manual.pdf"
                targets = [{"page": page, "leftPct": 60, "topPct": 72, "widthPct": 20} for page in (2, 4)]
                with patch.object(stamp_engine, "find_anchors", side_effect=AssertionError("Manual mode must not call OCR")):
                    result = stamp_engine.stamp_pdf(source, stamp, output, manual_targets=targets)
                self.assertEqual([p["page"] for p in result["placements"]], [2, 4])
                pages = PdfReader(output).pages
                self.assertEqual([len(p.images) for p in pages], [0, 1, 0, 1])
                for number in (1, 3):
                    self.assertEqual(pages[number].rotation, 0)
                    self.assertIn("TEST AUTOMATIZARE", pages[number].extract_text())
                    rect = result["placements"][0]["rect_top_left"]
                    self.assertAlmostEqual(rect["x0"], float(pages[number].mediabox.width)*0.6, delta=0.02)
                if rotation == 0 and os.environ.get("STAMP_SAFETY_EXPORT"):
                    destination = Path(os.environ["STAMP_SAFETY_EXPORT"])
                    destination.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(output, destination / "manual-stamp-synthetic.pdf")

    def test_invalid_targets_never_fall_back_to_automatic_stamp(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            source, stamp=self.source(root)
            for targets in ([], [{"page":5,"leftPct":60,"topPct":72,"widthPct":20}],
                            [{"page":1,"leftPct":90,"topPct":95,"widthPct":30}],
                            [{"page":1,"leftPct":float("nan"),"topPct":72,"widthPct":20}],
                            [{"page":True,"leftPct":60,"topPct":72,"widthPct":20}]):
                with self.subTest(targets=targets), self.assertRaises(ValueError):
                    stamp_engine.stamp_pdf(source,stamp,root/"invalid.pdf",manual_targets=targets)


if __name__ == "__main__":
    unittest.main()
