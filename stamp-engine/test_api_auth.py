from datetime import datetime, timezone
import unittest
from api_auth import authorized_key


class ApiAuthTest(unittest.TestCase):
    def test_primary_and_missing_key(self):
        self.assertTrue(authorized_key("primary", "primary"))
        for value in ("", None, "incorrect"):
            self.assertFalse(authorized_key(value, "primary"))
        self.assertFalse(authorized_key("anything", ""))

    def test_bounded_stage(self):
        now = 1793475000
        key = "n" * 48
        future = datetime.fromtimestamp(now + 300, timezone.utc).isoformat()
        self.assertTrue(authorized_key(key, "primary", key, future, now=now))
        self.assertFalse(authorized_key(key, "primary", key, future, now=now + 300))
        self.assertFalse(authorized_key(key, "primary", key, "invalid", now=now))
        self.assertFalse(authorized_key(key, "primary", key, now=now))
        far = datetime.fromtimestamp(now + 3601, timezone.utc).isoformat()
        self.assertFalse(authorized_key(key, "primary", key, far, now=now))
        self.assertTrue(authorized_key("primary", "primary", key, future, now=now + 300))
        self.assertFalse(authorized_key("\u0219", "primary"))


if __name__ == "__main__":
    unittest.main()
