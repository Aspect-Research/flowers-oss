"""flowers — a trustable agent with broad tool-use capability and a deterministic trust gate.

The gate (``flowers.trustgate``) is the load-bearing differentiator: a pure, no-LLM decision core that
refuses a claimed "done" resting on stale reads or on an external effect the world does not actually
reflect. Everything else attaches to that core.
"""

__version__ = "0.1.0"
