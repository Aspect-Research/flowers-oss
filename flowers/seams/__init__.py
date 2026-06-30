"""Seams — one Protocol per external dependency, each with an offline Fake and a live adapter.

The engine depends only on the Protocols in ``flowers.seams.interfaces``. The offline test suite
injects the Fakes; with keys set, the live adapters are used (they report ``available() is False`` unless
their credential is present and we are not forced offline — see ``flowers.runtime``). This is how
broad tool-use capability stays swappable AND the suite stays $0/no-network.
"""
