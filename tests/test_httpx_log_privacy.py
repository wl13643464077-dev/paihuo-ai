"""Request URLs must not enter production INFO logs via httpx."""

import logging
import unittest

from app import main  # noqa: F401 - installs the production logging policy


class HttpxLogPrivacyTests(unittest.TestCase):
    def test_info_request_url_is_suppressed(self):
        logger = logging.getLogger("httpx")
        self.assertGreaterEqual(logger.getEffectiveLevel(), logging.WARNING)

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        logger.addHandler(handler)
        try:
            logger.info("HTTP Request: GET https://example.invalid/search?q=customer-brand")
        finally:
            logger.removeHandler(handler)
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
