"""Deterministic (offline, no-LLM) extractors.

Importing this package imports each jurisdiction module, whose import side-effect is to register
its extractor with ``di.extract.base`` so ``get_extractor(doc_type)`` resolves them.
"""
from __future__ import annotations

from di.extract.deterministic import canada, mexico, mrz, us  # noqa: F401  (register on import)

__all__ = ["canada", "mexico", "mrz", "us"]
