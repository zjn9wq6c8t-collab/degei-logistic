from http import HTTPStatus
import unittest
from unittest.mock import Mock, patch

import api_server


class ApiFallbackContractTests(unittest.TestCase):
    def invoke(self, extra):
        payload = {
            "pdf_url": "https://example.invalid/order.pdf",
            "stamp_url": "https://example.invalid/stamp.png",
            **extra,
        }
        handler = object.__new__(api_server.StampHandler)
        handler.path = "/stamp"
        handler.authorized = lambda: True
        handler.read_json_body = lambda: payload
        handler.write_json = Mock()
        with (
            patch.object(api_server, "cleanup_old_files"),
            patch.object(api_server, "download_file") as download,
            patch.object(
                api_server,
                "stamp_pdf",
                return_value={"needs_review": True, "anchor_count": 0},
            ) as stamp,
        ):
            handler.do_POST()
        return handler.write_json, download, stamp

    def test_explicit_false_disables_fallback(self):
        response, download, stamp = self.invoke({"allow_fallback": False})
        self.assertIs(stamp.call_args.kwargs["allow_fallback"], False)
        self.assertEqual(download.call_count, 2)
        self.assertEqual(response.call_args.args[0], HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertTrue(response.call_args.args[1]["needs_review"])

    def test_explicit_true_keeps_fallback_enabled(self):
        _response, _download, stamp = self.invoke({"allow_fallback": True})
        self.assertIs(stamp.call_args.kwargs["allow_fallback"], True)

    def test_omitted_flag_preserves_existing_api_default(self):
        _response, _download, stamp = self.invoke({})
        self.assertIs(stamp.call_args.kwargs["allow_fallback"], True)

    def test_non_boolean_flags_are_rejected_before_processing(self):
        for value in ("false", "true", 0, 1, None, [], {}):
            with self.subTest(value=value):
                response, download, stamp = self.invoke({"allow_fallback": value})
                self.assertEqual(response.call_args.args[0], HTTPStatus.BAD_REQUEST)
                download.assert_not_called()
                stamp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
