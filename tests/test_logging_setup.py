from __future__ import annotations

import logging
import logging.handlers
import tempfile
import unittest
from pathlib import Path

from qq_live_digest.logging_setup import setup_logging


class LoggingSetupTest(unittest.TestCase):
    def setUp(self) -> None:
        root = logging.getLogger()
        self._root = root
        self._handlers = list(root.handlers)
        self._level = root.level
        self._botpy = logging.getLogger("botpy").level
        self._dir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._dir.name)

    def tearDown(self) -> None:
        for handler in list(self._root.handlers):
            self._root.removeHandler(handler)
            handler.close()
        for handler in self._handlers:
            self._root.addHandler(handler)
        self._root.setLevel(self._level)
        logging.getLogger("botpy").setLevel(self._botpy)
        self._dir.cleanup()

    def _file_handlers(self) -> list[logging.handlers.RotatingFileHandler]:
        return [h for h in self._root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]

    def test_file_handler_rotates_at_five_megabytes(self) -> None:
        setup_logging(self.log_dir, level=logging.INFO, console=False)
        handlers = self._file_handlers()
        self.assertEqual(1, len(handlers))
        handler = handlers[0]
        self.assertEqual(5 * 1024 * 1024, handler.maxBytes)
        self.assertEqual(5, handler.backupCount)
        self.assertEqual("qq-live-digest.log", Path(handler.baseFilename).name)

        logging.getLogger("test").info("hello log")
        handler.flush()
        self.assertIn("hello log", (self.log_dir / "qq-live-digest.log").read_text(encoding="utf-8"))

    def test_log_file_is_rotated_instead_of_growing_without_limit(self) -> None:
        setup_logging(self.log_dir, level=logging.INFO, console=False)
        handler = self._file_handlers()[0]
        handler.flush()
        line = "x" * 1024 + "\n"
        logger = logging.getLogger("test")
        for _ in range(6 * 1024):
            logger.info(line)
        handler.flush()

        rotated = self.log_dir / "qq-live-digest.log.1"
        self.assertTrue(rotated.exists(), "超过 5 MB 时应生成 .1 备份")
        self.assertLess((self.log_dir / "qq-live-digest.log").stat().st_size, handler.maxBytes)

    def test_repeated_setup_closes_previous_handlers(self) -> None:
        setup_logging(self.log_dir, level=logging.INFO, console=False)
        first = self._file_handlers()[0]
        logging.getLogger("test").info("before")
        first.flush()

        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        setup_logging(Path(other.name), level=logging.INFO, console=False)

        self.assertIsNone(first.stream, "旧 handler 应被关闭，避免句柄泄漏")
        handlers = self._file_handlers()
        self.assertEqual(1, len(handlers))
        self.assertEqual("qq-live-digest.log", Path(handlers[0].baseFilename).name)
        self.assertNotEqual(first, handlers[0])


if __name__ == "__main__":
    unittest.main()