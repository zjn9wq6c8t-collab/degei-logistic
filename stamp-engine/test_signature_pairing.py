from __future__ import annotations

import unittest

import api_server  # noqa: F401 - importing applies the production patch layer.
import stamp_engine


class SignaturePairingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page_w = 595.276
        self.page_h = 841.89
        self.header_anchor = stamp_engine.Anchor(
            page_index=0,
            box=stamp_engine.Box(130.39, 130.78, 227.09, 142.78),
            score=75,
            phrase="DEGEI LOGISTIC",
            line_text="Catre: DEGEI LOGISTIC S.R.L.",
        )
        self.signature_anchor = stamp_engine.Anchor(
            page_index=2,
            box=stamp_engine.Box(365.59, 170.67, 454.23, 181.67),
            score=90,
            phrase="DEGEI LOGISTIC",
            line_text="EUROPRIM INTERMED SRL DEGEI LOGISTIC S.R.L.",
        )
        self.client_stamp = stamp_engine.Box(184.25, 181.96, 283.46, 281.17)

    def test_selects_paired_signature_above_half_page(self) -> None:
        selected = stamp_engine.select_transporter_signature_anchors(
            [self.header_anchor, self.signature_anchor],
            [[], [], []],
            [[], [], [self.client_stamp]],
            [(self.page_w, self.page_h)] * 3,
        )
        self.assertEqual(selected, [self.signature_anchor])

    def test_aligns_stamp_below_company_and_with_client_stamp(self) -> None:
        words = [
            self.signature_anchor.box,
            stamp_engine.Box(350.0, 187.0, 510.0, 198.0),
        ]
        width, height = stamp_engine.stamp_size_for_anchor(
            self.signature_anchor,
            self.page_w,
            self.page_h,
            105.0,
            0.4,
            [self.client_stamp],
        )
        reason, rect = stamp_engine.choose_signature_block_candidate(
            self.signature_anchor,
            words,
            [self.client_stamp],
            self.page_w,
            self.page_h,
            width,
            height,
        )
        self.assertEqual(reason, "signature_block_align_client_stamp")
        self.assertGreaterEqual(rect.top, 200.0)
        self.assertLess(rect.top, 230.0)
        self.assertGreaterEqual(rect.x0, 350.0)
        self.assertLessEqual(width, 114.0)

    def test_selects_repeated_carrier_footer_on_every_page(self) -> None:
        first_footer = stamp_engine.Anchor(
            page_index=0,
            box=stamp_engine.Box(365.0, 760.0, 455.0, 772.0),
            score=140,
            phrase="DEGEI LOGISTIC",
            line_text="DEGEI LOGISTIC SRL",
        )
        last_footer = stamp_engine.Anchor(
            page_index=1,
            box=stamp_engine.Box(365.0, 780.0, 455.0, 792.0),
            score=155,
            phrase="DEGEI LOGISTIC",
            line_text="DEGEI LOGISTIC SRL",
        )
        client_stamps = [
            [stamp_engine.Box(80.0, 745.0, 165.0, 830.0)],
            [stamp_engine.Box(80.0, 755.0, 165.0, 838.0)],
        ]
        words = [
            [first_footer.box],
            [last_footer.box],
        ]

        selected = stamp_engine.select_transporter_signature_anchors(
            [first_footer, last_footer],
            words,
            client_stamps,
            [(self.page_w, self.page_h)] * 2,
        )
        self.assertEqual(selected, [first_footer, last_footer])

        placements = stamp_engine.choose_placements(
            [first_footer, last_footer],
            words,
            client_stamps,
            [(self.page_w, self.page_h)] * 2,
            stamp_w=105.0,
            stamp_ratio=0.4,
            allow_fallback=False,
        )
        self.assertEqual([placement.page_index for placement in placements], [0, 1])
        for placement, footer in zip(placements, [first_footer, last_footer]):
            self.assertGreaterEqual(placement.rect.top, footer.box.bottom + 2.0)
            self.assertGreater(placement.rect.x0, self.page_w * 0.50)
            self.assertLessEqual(placement.rect.bottom, self.page_h - 15.0)

    def test_explicit_signature_cell_overrides_generic_page_anchor(self) -> None:
        landscape_w = 841.89
        landscape_h = 420.95
        generic_page_anchor = stamp_engine.Anchor(
            page_index=0,
            box=stamp_engine.Box(20.0, 35.0, 115.0, 48.0),
            score=90,
            phrase="TRANSPORTATOR",
            line_text="TRANSPORTATOR",
        )
        company_anchor = stamp_engine.Anchor(
            page_index=0,
            box=stamp_engine.Box(520.0, 55.0, 620.0, 67.0),
            score=75,
            phrase="DEGEI LOGISTIC",
            line_text="DEGEI LOGISTIC SRL",
        )
        left_signature = stamp_engine.Anchor(
            page_index=1,
            box=stamp_engine.Box(24.0, 250.0, 170.0, 264.0),
            score=130,
            phrase="SEMNATURA SI STAMPILA",
            line_text="Semnatura si stampila",
        )
        right_signature = stamp_engine.Anchor(
            page_index=1,
            box=stamp_engine.Box(455.0, 250.0, 610.0, 264.0),
            score=130,
            phrase="SEMNATURA SI STAMPILA",
            line_text="Semnatura si stampila",
        )
        left_company = stamp_engine.Box(24.0, 270.0, 210.0, 284.0)
        left_phone = stamp_engine.Box(24.0, 320.0, 110.0, 334.0)
        anchors = [generic_page_anchor, company_anchor, left_signature, right_signature]
        words = [
            [generic_page_anchor.box, company_anchor.box],
            [left_signature.box, right_signature.box, left_company, left_phone],
        ]
        page_sizes = [(landscape_w, landscape_h)] * 2

        selected = stamp_engine.select_explicit_signature_anchors(
            anchors,
            words,
            page_sizes,
        )
        self.assertEqual(selected, [right_signature])

        placements = stamp_engine.choose_placements(
            anchors,
            words,
            [[], []],
            page_sizes,
            stamp_w=105.0,
            stamp_ratio=0.4,
            allow_fallback=False,
        )
        self.assertEqual(len(placements), 1)
        self.assertEqual(placements[0].page_index, 1)
        self.assertEqual(placements[0].anchor_phrase, "SEMNATURA SI STAMPILA")
        self.assertGreater(placements[0].rect.x0, landscape_w * 0.50)
        self.assertGreaterEqual(placements[0].rect.top, right_signature.box.bottom + 4.0)

    def test_finds_repeated_signature_labels_in_two_columns(self) -> None:
        words = [
            {"text": "Semnatura", "x0": 24.0, "top": 250.0, "x1": 88.0, "bottom": 264.0},
            {"text": "si", "x0": 92.0, "top": 250.0, "x1": 103.0, "bottom": 264.0},
            {"text": "stampila", "x0": 107.0, "top": 250.0, "x1": 170.0, "bottom": 264.0},
            {"text": "Semnatura", "x0": 455.0, "top": 250.0, "x1": 519.0, "bottom": 264.0},
            {"text": "si", "x0": 523.0, "top": 250.0, "x1": 534.0, "bottom": 264.0},
            {"text": "stampila", "x0": 538.0, "top": 250.0, "x1": 601.0, "bottom": 264.0},
        ]

        matches = stamp_engine.phrase_boxes(words, "SEMNATURA SI STAMPILA")

        self.assertEqual(len(matches), 2)
        self.assertLess(matches[0].x1, matches[1].x0)

    def test_repeated_two_column_signature_line_is_a_valid_label(self) -> None:
        label = "SEMNATURA SI STAMPILA"
        box = stamp_engine.Box(47.0, 245.0, 984.0, 261.0)

        self.assertTrue(
            stamp_engine.looks_like_carrier_signature_label(
                f"{label} {label}",
                label,
                box,
                self.page_h,
            )
        )
        self.assertFalse(
            stamp_engine.looks_like_carrier_signature_label(
                f"{label} INCARCATORULUI DESTINATARULUI",
                label,
                box,
                self.page_h,
            )
        )

    def test_explicit_carrier_label_allows_overlap_on_every_matching_page(self) -> None:
        phrase = "STAMPILA SI SEMNATURA TRANSPORTATORULUI"
        anchors = [
            stamp_engine.Anchor(
                page_index=page_index,
                box=stamp_engine.Box(380.0, 790.0, 570.0, 804.0),
                score=170,
                phrase=phrase,
                line_text="Stampila si semnatura transportatorului",
            )
            for page_index in range(2)
        ]
        blocking_text = [
            [
                anchor.box,
                stamp_engine.Box(390.0, 806.0, 560.0, 830.0),
            ]
            for anchor in anchors
        ]

        placements = stamp_engine.choose_placements(
            anchors,
            blocking_text,
            [[], []],
            [(self.page_w, self.page_h)] * 2,
            stamp_w=105.0,
            stamp_ratio=0.4,
            allow_fallback=False,
        )

        self.assertEqual([placement.page_index for placement in placements], [0, 1])
        for placement in placements:
            self.assertEqual(placement.anchor_phrase, phrase)
            self.assertEqual(
                placement.reason,
                "explicit_carrier_signature_overlap_allowed",
            )
            self.assertGreater(placement.rect.x0, self.page_w * 0.50)
            self.assertLessEqual(placement.rect.bottom, self.page_h - 12.0)

    def test_split_transportator_degei_footer_stamps_every_matching_page(self) -> None:
        anchors = []
        words = [[], [], []]
        for page_index in (1, 2):
            transportator = stamp_engine.Anchor(
                page_index=page_index,
                box=stamp_engine.Box(462.0, 790.0, 548.0, 802.0),
                score=98,
                phrase="TRANSPORTATOR",
                line_text="Transportator",
            )
            company = stamp_engine.Anchor(
                page_index=page_index,
                box=stamp_engine.Box(454.0, 805.0, 555.0, 817.0),
                score=178,
                phrase="DEGEI LOGISTIC",
                line_text="DEGEI LOGISTIC",
            )
            anchors.extend((transportator, company))
            words[page_index].extend(
                (
                    transportator.box,
                    company.box,
                    stamp_engine.Box(510.0, 819.0, 548.0, 830.0),
                )
            )

        selected = stamp_engine.select_transporter_signature_anchors(
            anchors,
            words,
            [[], [], []],
            [(self.page_w, self.page_h)] * 3,
        )
        self.assertEqual([anchor.page_index for anchor in selected], [1, 2])

        placements = stamp_engine.choose_placements(
            anchors,
            words,
            [[], [], []],
            [(self.page_w, self.page_h)] * 3,
            stamp_w=105.0,
            stamp_ratio=0.40,
            allow_fallback=True,
        )

        self.assertEqual([placement.page_index for placement in placements], [1, 2])
        for placement in placements:
            self.assertEqual(placement.anchor_phrase, "DEGEI LOGISTIC")
            self.assertIn(
                placement.reason,
                {
                    "carrier_label_immediately_below",
                    "carrier_label_controlled_overlap",
                },
            )
            self.assertGreater(placement.rect.x0, self.page_w * 0.50)
            self.assertLessEqual(placement.rect.bottom, self.page_h - 12.0)

    def test_company_identity_header_is_not_a_signature_zone_even_with_image(self) -> None:
        header_company = stamp_engine.Anchor(
            page_index=0,
            box=stamp_engine.Box(146.0, 180.0, 219.0, 189.0),
            score=178,
            phrase="DEGEI LOGISTIC",
            line_text="Transportator: Nume Firma: DEGEI LOGISTIC S.R.L.",
        )
        header_tax_id = stamp_engine.Anchor(
            page_index=0,
            box=stamp_engine.Box(113.0, 194.0, 166.0, 203.0),
            score=120,
            phrase="RO36256981",
            line_text="CIF: RO36256981",
        )
        anchors = [header_company, header_tax_id]
        for page_index, top in ((1, 741.0), (2, 759.0)):
            anchors.extend(
                (
                    stamp_engine.Anchor(
                        page_index=page_index,
                        box=stamp_engine.Box(411.0, top - 23.0, 480.0, top - 13.0),
                        score=98,
                        phrase="TRANSPORTATOR",
                        line_text="Transportator",
                    ),
                    stamp_engine.Anchor(
                        page_index=page_index,
                        box=stamp_engine.Box(411.0, top, 484.0, top + 9.0),
                        score=178,
                        phrase="DEGEI LOGISTIC",
                        line_text="DEGEI LOGISTIC",
                    ),
                )
            )

        selected = stamp_engine.select_transporter_signature_anchors(
            anchors,
            [[], [], []],
            [[stamp_engine.Box(24.0, 160.0, 110.0, 225.0)], [], []],
            [(self.page_w, self.page_h)] * 3,
        )

        self.assertEqual([anchor.page_index for anchor in selected], [1, 2])

    def test_split_carrier_pair_can_be_in_middle_of_page(self) -> None:
        transportator = stamp_engine.Anchor(
            page_index=0,
            box=stamp_engine.Box(410.0, 360.0, 502.0, 372.0),
            score=98,
            phrase="TRANSPORTATOR",
            line_text="Transportator",
        )
        company = stamp_engine.Anchor(
            page_index=0,
            box=stamp_engine.Box(410.0, 378.0, 520.0, 390.0),
            score=83,
            phrase="DEGEI LOGISTIC",
            line_text="DEGEI LOGISTIC S.R.L.",
        )

        selected = stamp_engine.select_transporter_signature_anchors(
            [transportator, company],
            [[transportator.box, company.box]],
            [[]],
            [(self.page_w, self.page_h)],
        )

        self.assertEqual(selected, [company])

    def test_caraus_degei_pair_in_upper_third_selects_only_that_page(self) -> None:
        header_company = stamp_engine.Anchor(
            page_index=0,
            box=stamp_engine.Box(64.53, 208.49, 141.08, 217.99),
            score=75,
            phrase="DEGEI LOGISTIC",
            line_text="DEGEI LOGISTIC SRL",
        )
        header_tax_id = stamp_engine.Anchor(
            page_index=0,
            box=stamp_engine.Box(64.53, 222.07, 109.14, 229.57),
            score=70,
            phrase="RO36256981",
            line_text="RO36256981 0039 3248603817 B998DGI / DB27DGI",
        )
        caraus = stamp_engine.Anchor(
            page_index=3,
            box=stamp_engine.Box(303.64, 232.49, 336.90, 241.99),
            score=115,
            phrase="CARAUS",
            line_text="Beneficiar: Caraus:",
        )
        carrier_company = stamp_engine.Anchor(
            page_index=3,
            box=stamp_engine.Box(303.64, 250.49, 380.19, 259.99),
            score=90,
            phrase="DEGEI LOGISTIC",
            line_text="SC HYDRAS SRL DEGEI LOGISTIC SRL",
        )
        anchors = [header_company, header_tax_id, caraus, carrier_company]
        words = [
            [header_company.box, header_tax_id.box],
            [],
            [],
            [caraus.box, carrier_company.box],
        ]

        placements = stamp_engine.choose_placements(
            anchors,
            words,
            [[], [], [], []],
            [(self.page_w, self.page_h)] * 4,
            stamp_w=105.0,
            stamp_ratio=0.40,
            allow_fallback=True,
        )

        self.assertEqual(len(placements), 1)
        self.assertEqual(placements[0].page_index, 3)
        self.assertEqual(placements[0].anchor_phrase, "DEGEI LOGISTIC")
        self.assertGreaterEqual(placements[0].rect.top, carrier_company.box.bottom + 2.0)
        self.assertGreater(placements[0].rect.x0, self.page_w * 0.50)

    def test_bare_caraus_role_does_not_suppress_all_page_fallback(self) -> None:
        caraus = stamp_engine.Anchor(
            page_index=2,
            box=stamp_engine.Box(350.0, 260.0, 390.0, 272.0),
            score=100,
            phrase="CARAUS",
            line_text="Caraus",
        )
        page_count = 4

        placements = stamp_engine.choose_placements(
            [caraus],
            [[], [], [caraus.box], []],
            [[] for _ in range(page_count)],
            [(self.page_w, self.page_h)] * page_count,
            stamp_w=105.0,
            stamp_ratio=0.40,
            allow_fallback=True,
        )

        self.assertEqual(
            [placement.page_index for placement in placements],
            list(range(page_count)),
        )
        self.assertTrue(
            all(placement.anchor_phrase == "FALLBACK_EACH_PAGE" for placement in placements)
        )

    def test_caraus_with_another_company_does_not_select_a_page(self) -> None:
        caraus = stamp_engine.Anchor(
            page_index=1,
            box=stamp_engine.Box(350.0, 260.0, 390.0, 272.0),
            score=100,
            phrase="CARAUS",
            line_text="Caraus",
        )

        selected = stamp_engine.select_transporter_signature_anchors(
            [caraus],
            [[], [caraus.box]],
            [[], []],
            [(self.page_w, self.page_h)] * 2,
        )

        self.assertIsNone(selected)

    def test_no_anchor_fallback_stamps_every_page_bottom_right(self) -> None:
        page_count = 4
        placements = stamp_engine.choose_placements(
            [],
            [[] for _ in range(page_count)],
            [[] for _ in range(page_count)],
            [(self.page_w, self.page_h)] * page_count,
            stamp_w=105.0,
            stamp_ratio=0.40,
            allow_fallback=True,
        )

        self.assertEqual(
            [placement.page_index for placement in placements],
            list(range(page_count)),
        )
        for placement in placements:
            self.assertEqual(placement.anchor_phrase, "FALLBACK_EACH_PAGE")
            self.assertGreater(placement.rect.x0, self.page_w * 0.50)
            self.assertGreater(placement.rect.top, self.page_h * 0.60)
            self.assertLessEqual(placement.rect.bottom, self.page_h - 28.0)

    def test_identity_header_anchors_do_not_suppress_all_page_fallback(self) -> None:
        company = stamp_engine.Anchor(
            page_index=0,
            box=stamp_engine.Box(297.75, 26.90, 383.07, 36.90),
            score=75,
            phrase="DEGEI LOGISTIC",
            line_text="GOPET TRANS EOOD DEGEI LOGISTIC SRL",
        )
        tax_id = stamp_engine.Anchor(
            page_index=0,
            box=stamp_engine.Box(297.75, 38.90, 363.45, 48.90),
            score=70,
            phrase="RO36256981",
            line_text="VAT: BG831689341 RO36256981",
        )
        page_count = 4

        placements = stamp_engine.choose_placements(
            [company, tax_id],
            [[company.box, tax_id.box], [], [], []],
            [[] for _ in range(page_count)],
            [(self.page_w, self.page_h)] * page_count,
            stamp_w=105.0,
            stamp_ratio=0.40,
            allow_fallback=True,
        )

        self.assertEqual(
            [placement.page_index for placement in placements],
            list(range(page_count)),
        )
        for placement in placements:
            self.assertEqual(placement.anchor_phrase, "FALLBACK_EACH_PAGE")
            self.assertTrue(placement.reason.startswith("fallback_bottom_right_"))
            self.assertGreater(placement.rect.x0, self.page_w * 0.50)
            self.assertGreater(placement.rect.top, self.page_h * 0.60)

    def test_dedicated_pages_do_not_trigger_fallback_on_other_pages(self) -> None:
        transportator = stamp_engine.Anchor(
            page_index=1,
            box=stamp_engine.Box(455.0, 730.0, 545.0, 742.0),
            score=98,
            phrase="TRANSPORTATOR",
            line_text="Transportator",
        )
        company = stamp_engine.Anchor(
            page_index=1,
            box=stamp_engine.Box(455.0, 748.0, 555.0, 760.0),
            score=178,
            phrase="DEGEI LOGISTIC",
            line_text="DEGEI LOGISTIC S.R.L.",
        )
        words = [[], [transportator.box, company.box], []]

        placements = stamp_engine.choose_placements(
            [transportator, company],
            words,
            [[], [], []],
            [(self.page_w, self.page_h)] * 3,
            stamp_w=105.0,
            stamp_ratio=0.40,
            allow_fallback=True,
        )

        self.assertEqual([placement.page_index for placement in placements], [1])

    def test_legal_signature_sentence_does_not_override_carrier_block(self) -> None:
        legal_reference = stamp_engine.Anchor(
            page_index=3,
            box=stamp_engine.Box(45.35, 218.02, 123.15, 226.02),
            score=115,
            phrase="SEMNATURA SI STAMPILA",
            line_text="semnatura si stampila incarcatorului/destinararului.",
        )
        carrier = stamp_engine.Anchor(
            page_index=4,
            box=stamp_engine.Box(380.18, 587.80, 452.71, 596.80),
            score=193,
            phrase="DEGEI LOGISTIC",
            line_text="Dacoda S.R.L. DEGEI LOGISTIC SRL",
        )
        client_stamp = stamp_engine.Box(155.91, 574.88, 246.61, 665.59)
        words = [[] for _ in range(5)]
        words[3] = [legal_reference.box]
        words[4] = [
            carrier.box,
            stamp_engine.Box(379.0, 601.0, 456.0, 610.0),
            stamp_engine.Box(379.0, 612.0, 449.0, 621.0),
        ]
        images = [[] for _ in range(5)]
        images[4] = [client_stamp]
        page_sizes = [(self.page_w, self.page_h)] * 5

        self.assertFalse(
            stamp_engine.looks_like_carrier_signature_label(
                stamp_engine.norm(legal_reference.line_text),
                legal_reference.phrase,
                legal_reference.box,
                self.page_h,
            )
        )

        placements = stamp_engine.choose_placements(
            [legal_reference, carrier],
            words,
            images,
            page_sizes,
            stamp_w=105.0,
            stamp_ratio=0.40,
            allow_fallback=True,
        )

        self.assertEqual(len(placements), 1)
        self.assertEqual(placements[0].page_index, 4)
        self.assertEqual(placements[0].anchor_phrase, "DEGEI LOGISTIC")
        self.assertEqual(placements[0].reason, "signature_block_align_client_stamp")
        self.assertGreater(placements[0].rect.x0, self.page_w * 0.50)


if __name__ == "__main__":
    unittest.main()
