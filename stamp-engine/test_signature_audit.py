from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
from reportlab.pdfgen import canvas
import stamp_engine as engine


class SignatureAuditTests(unittest.TestCase):
    def extract(self, lines):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'order.pdf'
            pdf = canvas.Canvas(str(path), pagesize=(595, 842))
            pdf.setFont('Helvetica', 8)
            for x, top, text in lines:
                pdf.drawString(x, 842 - top, text)
            pdf.save()
            with patch.object(engine, 'ocr_page_words', return_value=[]):
                return engine.find_anchors(path)

    def test_contractual_company_mention_is_not_a_signing_anchor(self):
        anchors, *_ = self.extract([
            (25, 170, 'q) In cazul in care imi sunt opozabile, subscrisa Degei Logistic S.R.L. declar si asigur')
        ])
        self.assertFalse(any(a.phrase == 'DEGEI LOGISTIC' for a in anchors))

    def test_header_logo_is_not_a_reference_stamp(self):
        anchor = engine.Anchor(0, engine.Box(348, 148, 419, 160), 75,
                               'DEGEI LOGISTIC', 'DEGEI LOGISTIC SRL')
        logo = engine.Box(34, 8, 157, 71)
        self.assertIsNone(engine.reference_stamp_box(anchor, [logo]))
        self.assertFalse(engine.is_paired_signature_anchor(anchor, [logo], 595, 842))

    def test_confirmation_heading_selects_last_page_without_all_page_fallback(self):
        anchor = engine.Anchor(5, engine.Box(298, 550, 403, 559), 143,
                               'CONFIRMARE TRANSPORTATOR', 'Confirmare Transportator')
        fields = [engine.Box(298, 566, 360, 575), engine.Box(298, 582, 345, 590),
                  engine.Box(298, 597, 330, 605)]
        placements = engine.choose_placements([anchor], [[]]*5 + [[anchor.box, *fields]],
            [[] for _ in range(6)], [(595, 842)]*6, 102, 0.68, True)
        self.assertEqual([p.page_index for p in placements], [5])
        self.assertEqual(placements[0].reason, 'explicit_below_carrier_fields')
        self.assertGreaterEqual(placements[0].rect.top, 607)
        self.assertLess(placements[0].rect.top, 620)

    def test_page_counter_does_not_hide_right_hand_signature(self):
        anchors, words, images, sizes = self.extract([
            (20, 755, 'Semnatura si stampila'),
            (20, 766, 'MOORHOUSE LOGISTICS LTD SRL'),
            (275, 778, 'Page 1 of 2'),
            (470, 778, 'Semnatura si stampila'),
            (391, 788, '(TRANSPORTATOR)'),
        ])
        labels = [a for a in anchors if a.phrase == 'SEMNATURA SI STAMPILA']
        self.assertEqual(len(labels), 2)
        chosen = engine.select_explicit_signature_anchors(anchors, words, sizes, images)
        self.assertGreater(chosen[0].box.x0, 400)

    def test_confirmation_in_legal_prose_is_not_a_caption(self):
        anchors, *_ = self.extract([
            (20, 550, 'Prezentul contract se considera acceptat chiar si fara confirmarea transportatorului')
        ])
        self.assertFalse(any(a.phrase in engine.SIGNATURE_LABEL_TARGETS for a in anchors))

    def test_standalone_confirmation_needs_carrier_fields(self):
        anchors, *_ = self.extract([(400, 680, 'CONFIRMARE')])
        self.assertFalse(anchors)
        anchors, *_ = self.extract([(400, 680, 'CONFIRMARE'),
            (400, 700, 'Auto:'), (400, 720, 'Sofer:')])
        self.assertTrue(any(a.phrase == 'CONFIRMARE' for a in anchors))

    def test_repeated_confirmation_cells_keep_both_pages(self):
        anchors = [engine.Anchor(i, engine.Box(350, 650, 490, 660), 145,
            'CONFIRMARE TRANSPORTATOR', 'Confirmare transportator') for i in range(2)]
        placements = engine.choose_placements(anchors, [[a.box] for a in anchors],
            [[], []], [(595, 842)]*2, 102, 0.68, True)
        self.assertEqual([p.page_index for p in placements], [0, 1])

    def test_blank_gray_scanned_paper_is_not_ink(self):
        paper = Image.new('RGB', (595, 842), (213, 213, 213))
        rect = engine.Box(450, 720, 552, 790)
        self.assertTrue(engine.is_safe_rect(rect, [], paper, 595, 842))
        ImageDraw.Draw(paper).rectangle((450, 720, 552, 790), fill='black')
        self.assertFalse(engine.is_safe_rect(rect, [], paper, 595, 842))

    def test_full_text_page_never_gets_unverified_fallback_stamp(self):
        full = engine.Box(0, 0, 595, 842)
        self.assertIsNone(engine.choose_fallback_page_candidate([full], 595, 842, 102, .68))
        placements = engine.choose_placements([], [[], [full]], [[], []],
            [(595, 842)] * 2, 102, .68, True)
        self.assertFalse(placements)

    def test_form_rule_does_not_expand_the_printed_label_collision_box(self):
        _, words, _, _ = self.extract([(300, 750, 'Sofer:_________________________________')])
        self.assertLess(words[0][0].x1, 335)

    def test_split_ocr_confirmation_needs_supporting_carrier_fields(self):
        anchors, *_ = self.extract([(350, 650, 'CONFIRMARE re transport'),
                                    (350, 670, 'SC'), (350, 690, 'Director')])
        self.assertTrue(any(a.phrase == 'CONFIRMARE' for a in anchors))
        anchors, *_ = self.extract([(350, 650, 'CONFIRMARE re transport')])
        self.assertFalse(anchors)


if __name__ == '__main__':
    unittest.main()
