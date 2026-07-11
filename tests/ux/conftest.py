"""tests/ux — the conversation-replay eval harness (P2 / §5).

This conftest does exactly one thing the parent suite does not guarantee for a nested directory:
put ``tests/`` on ``sys.path`` so ``from _harness import make_brain, tc`` resolves regardless of
collection order or of ``pytest tests/ux/`` being run in isolation. The offline/$0/no-network
contract is inherited unchanged from the ROOT conftest (``FLOWERS_FORCE_OFFLINE=1`` + blanked
provider keys), so a green ux suite is a $0, no-network suite like the rest.
"""

from __future__ import annotations

import os
import sys

# tests/ (the directory holding _harness.py) — its PARENT is this file's grandparent.
_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
