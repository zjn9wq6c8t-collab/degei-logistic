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

    def test_upper_page_company_signoff_is_selected_below_name(self):
        anchors, words, images, sizes = self.extract([
            (80, 164, 'Va solicitam in mod obligatoriu sa confirmati prezenta comanda cu semnatura si stampila!'),
            (25, 201, 'GETIX BLUE SRL'),
            (376, 201, 'DEGEI LOGISTIC S.R.L.'),
            (425, 213, '.'),
        ])
        company = next(a for a in anchors if a.phrase == 'DEGEI LOGISTIC')
        self.assertTrue(company.signature_context)
        placements = engine.choose_placements(anchors, words, images, sizes, 105, .68, True)
        self.assertEqual(len(placements), 1)
        self.assertEqual(placements[0].anchor_phrase, 'DEGEI LOGISTIC')
        self.assertGreaterEqual(placements[0].rect.top, company.box.bottom + 2)
        self.assertLess(placements[0].rect.top, company.box.bottom + 25)
        self.assertLess(engine.overlap_ratio(placements[0].rect, words[0]), .001)

    def test_upper_company_without_order_signoff_stays_unverified(self):
        for instruction in (
            'Va rugam sa confirmati CMR cu semnatura si stampila!',
            'Comanda neconfirmata prin semnatura si stampila nu se plateste.',
            'Va rugam sa confirmati factura cu semnatura si stampila!',
            'In cazul in care va rugam sa confirmati comanda cu semnatura si stampila',
            'Date de facturare',
        ):
            with self.subTest(instruction=instruction):
                anchors, *_ = self.extract([(80, 164, instruction),
                                           (376, 201, 'DEGEI LOGISTIC S.R.L.')])
                self.assertFalse(any(a.signature_context for a in anchors))

    def test_signoff_does_not_turn_recipient_header_into_signature(self):
        anchors, *_ = self.extract([
            (80, 164, 'Va rugam sa confirmati prezenta comanda cu semnatura si stampila!'),
            (376, 201, 'Catre: DEGEI LOGISTIC SRL'),
        ])
        self.assertFalse(any(a.signature_context for a in anchors))

    def test_signoff_context_cannot_cross_to_another_column(self):
        anchors, *_ = self.extract([
            (20, 164, 'Va rugam confirmati comanda cu semnatura si stampila'),
            (440, 201, 'DEGEI LOGISTIC SRL'),
        ])
        self.assertFalse(any(a.signature_context for a in anchors))

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

    def test_dense_page_uses_authorized_all_page_fallback(self):
        full = engine.Box(0, 0, 595, 842)
        self.assertIsNone(engine.choose_fallback_page_candidate([full], 595, 842, 102, .68))
        placements = engine.choose_placements([], [[], [full]], [[], []],
            [(595, 842)] * 2, 102, .68, True)
        self.assertEqual([p.page_index for p in placements], [0, 1])
        self.assertEqual(placements[1].reason, 'fallback_bottom_right_authorized_overlap')

    def test_split_carrier_signature_labels_select_the_final_page(self):
        anchors, words, images, sizes = self.extract([
            (25, 241, 'SC IB CARGO SRL'), (417, 241, 'Transportator'),
            (417, 259, 'SEMNATURA'), (417, 277, 'STAMPILA'),
        ])
        explicit = [a for a in anchors if a.phrase == 'SEMNATURA SI STAMPILA TRANSPORTATOR']
        self.assertEqual(len(explicit), 1)
        placements = engine.choose_placements(anchors, words, images, sizes, 105, .68, True)
        self.assertEqual(len(placements), 1)
        self.assertEqual(placements[0].anchor_phrase, explicit[0].phrase)
        self.assertGreaterEqual(placements[0].rect.top, explicit[0].box.bottom)
        self.assertLess(engine.overlap_ratio(placements[0].rect, words[0]), .001)

    def test_split_fields_never_cross_columns_or_use_legal_prose(self):
        cases = [
            [(417, 241, 'Transportator'), (25, 259, 'SEMNATURA'), (25, 277, 'STAMPILA')],
            [(417, 241, 'Destinatar'), (417, 259, 'SEMNATURA'), (417, 277, 'STAMPILA')],
            [(417, 241, 'Transportator'), (417, 259, 'SEMNATURA'), (417, 277, 'STAMPILA pe CMR este obligatorie')],
            [(417, 241, 'Transportator'), (417, 259, 'SEMNATURA'), (417, 350, 'STAMPILA')],
            [(417, 241, 'Transportator')],
        ]
        for lines in cases:
            with self.subTest(lines=lines):
                anchors, *_ = self.extract(lines)
                self.assertFalse(any(a.phrase == 'SEMNATURA SI STAMPILA TRANSPORTATOR' for a in anchors))

    def test_authorized_overlap_stays_in_corner_and_prefers_larger_stamp(self):
        for page_w, page_h in ((595, 842), (842, 595), (612, 792)):
            with self.subTest(size=(page_w, page_h)):
                full = engine.Box(0, 0, page_w, page_h)
                reason, box = engine.choose_fallback_page_candidate(
                    [full], page_w, page_h, 102, .68, allow_text_overlap=True)
                self.assertEqual(reason, 'fallback_bottom_right_authorized_overlap')
                self.assertEqual(box.width, 102)
                self.assertGreaterEqual(box.x0, page_w * .60)
                self.assertGreaterEqual(box.top, page_h * .75)
                self.assertLessEqual(box.x1, page_w - 12)
                self.assertLessEqual(box.bottom, page_h - 12)

    def test_fallback_still_prefers_clear_space_without_overlap(self):
        reason, box = engine.choose_fallback_page_candidate(
            [], 595, 842, 102, .68, allow_text_overlap=True)
        self.assertNotIn('authorized_overlap', reason)
        self.assertEqual(engine.overlap_ratio(box, []), 0)

    def test_overlap_policy_does_not_override_fallback_disabled(self):
        full = engine.Box(0, 0, 595, 842)
        self.assertEqual(engine.choose_placements([], [[full]], [[]], [(595, 842)],
                                                 102, .68, False), [])

    def test_dense_contract_can_use_clear_compact_footer_on_every_page(self):
        page_w, page_h = 612, 792
        content = engine.Box(55, 58, 556, 729)
        footer = engine.Box(20, 768, 138, 776)
        page = Image.new('RGB', (page_w, page_h), 'white')
        draw = ImageDraw.Draw(page)
        for box in (content, footer):
            draw.rectangle((box.x0, box.top, box.x1, box.bottom), fill='black')
        words = [content, footer]
        self.assertIsNone(engine.choose_fallback_page_candidate(words, page_w, page_h, 102, .68))

        placements = engine.choose_placements([], [[], words], [[], []],
            [(page_w, page_h)] * 2, 105, .68, True, {1: page})
        self.assertEqual([p.page_index for p in placements], [0, 1])
        compact = placements[1]
        self.assertEqual(compact.reason, 'fallback_bottom_right_compact')
        self.assertGreaterEqual(compact.rect.top, content.bottom + 3)
        self.assertGreaterEqual(compact.rect.x0, page_w * .55)
        self.assertGreaterEqual(compact.rect.width, 64)
        self.assertLessEqual(compact.rect.bottom, page_h - 10)
        self.assertFalse(any(engine.overlap_area(compact.rect, word) for word in words))

        draw.rectangle((400, 730, page_w, page_h), fill='black')
        self.assertIsNone(engine.choose_fallback_page_candidate(words, page_w, page_h, 102, .68, page))

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
