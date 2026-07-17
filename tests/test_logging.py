import logging

from src.logging import setup_logging, get_logger


def test_get_logger_returns_namespaced_logger():
    setup_logging("DEBUG")
    log = get_logger("sub.module")
    assert log.name == "nl2sql.sub.module"
    assert log.getEffectiveLevel() == logging.DEBUG
