"""统一日志：把 handler 挂到 root 一次，所有 module logger 向上冒泡即可输出。"""
from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


def _ensure_root_handler() -> None:
    root = logging.getLogger()
    if any(getattr(h, "_pa", False) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.set_name("polymarket_arb")
    handler._pa = True  # type: ignore[attr-defined]
    root.addHandler(handler)


def get_logger(name: str = "polymarket_arb") -> logging.Logger:
    """返回一个会输出到 stdout 的 logger（任意命名均可）。"""
    _ensure_root_handler()
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = True  # 冒泡到已挂 handler 的 root
    return logger