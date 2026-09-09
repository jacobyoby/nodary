"""Public Suffix List identity helper.

Returns a stable identity string for the bundled PSL snapshot shipped
with ``tldextract``.  Used to detect when a ``tldextract`` upgrade
changes the suffix list, which may shift domain-extraction results and
therefore sender profiles / trust tiers.
"""

from __future__ import annotations

import hashlib
import importlib.resources

import tldextract


def get_current_psl_identity() -> str:
    """Return a stable identity string for the current PSL snapshot.

    Format: ``tldextract-{version}:{sha256[:12]}``

    The hash covers the bundled snapshot bytes only — not the runtime
    cache — so it is deterministic across machines running the same
    ``tldextract`` version.
    """
    version = tldextract.__version__
    ref = importlib.resources.files("tldextract").joinpath(".tld_set_snapshot")
    psl_hash = hashlib.sha256(ref.read_bytes()).hexdigest()[:12]
    return f"tldextract-{version}:{psl_hash}"
