from datetime import datetime
import secrets
import time


def authorized_key(supplied, primary, next_key="", next_until="", previous="", previous_until="", now=None):
    if not primary or not supplied or len(supplied) > 512:
        return False
    now = time.time() if now is None else now

    def matches(expected):
        return secrets.compare_digest(str(supplied).encode("utf-8"), str(expected).encode("utf-8"))

    def staged(key, until):
        try:
            expiry = datetime.fromisoformat(until.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, AttributeError):
            return False
        return len(key) >= 32 and 0 < expiry - now <= 3600 and matches(key)

    return matches(primary) or staged(next_key, next_until) or staged(previous, previous_until)
