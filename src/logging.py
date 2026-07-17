"""统一日志。所有 logger 挂在 nl2sql 命名空间下。"""
import logging
import sys

_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    root = logging.getLogger("nl2sql")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(handler)
        _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not name.startswith("nl2sql"):
        name = f"nl2sql.{name}"
    return logging.getLogger(name)
