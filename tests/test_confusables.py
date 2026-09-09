"""Tests for UTS #39 confusables generation and expanded lookalike coverage.

Covers:
- Generated map loads correctly from the vendored data.
- Latin/Cyrillic homoglyph domain detection (e.g. аррӏе.com ≈ apple.com).
- Digit substitution detection (e.g. g00gle.com ≈ google.com).
- UTS #39 catches confusables the curated subset alone misses.
- Runtime does not fetch anything from the network.
"""

from __future__ import annotations

import socket

from nodary.feature_extraction.normalize import (
    _CONFUSABLES,
    _CONFUSABLES_CURATED,
    skeleton,
)

# ------------------------------------------------------------------ loading --


def test_generated_map_is_loaded():
    """The merged _CONFUSABLES dict contains far more entries than the
    curated subset alone, proving the generated UTS #39 map was loaded."""
    assert len(_CONFUSABLES) > len(_CONFUSABLES_CURATED)
    # UTS #39 adds thousands of mappings; curated has ~40.
    assert len(_CONFUSABLES) > 1000


def test_curated_entries_survive_merge():
    """Every curated mapping is present in the merged map (overrides)."""
    for key, val in _CONFUSABLES_CURATED.items():
        assert _CONFUSABLES[key] == val, (
            f"curated override for {repr(key)} lost: "
            f"expected {repr(val)}, got {repr(_CONFUSABLES[key])}"
        )


def test_no_network_at_runtime():
    """skeleton() and the confusables loader must not touch the network.

    We block all socket connections and verify skeleton still works.
    """
    original_connect = socket.socket.connect

    def _blocked_connect(*args, **kwargs):
        raise RuntimeError("network access blocked during skeleton()")

    socket.socket.connect = _blocked_connect
    try:
        # These must succeed without any network access.
        # UTS #39 maps m→rn, so skeleton("apple.com") = "apple.corn".
        # The important invariant: same input → same skeleton, no network.
        assert skeleton("apple.com") == skeleton("apple.com")
        assert skeleton("g00gle.com") == skeleton("google.com")
    finally:
        socket.socket.connect = original_connect


# --------------------------------------------------- Cyrillic homoglyphs ---


def test_cyrillic_apple_skeleton():
    """Cyrillic 'аррӏе' should skeleton-match Latin 'apple'.

    а = U+0430 (Cyrillic а) → a
    р = U+0440 (Cyrillic р)  → p
    ӏ = U+04CF (Cyrillic palochka) → l (via UTS #39, not curated)
    е = U+0435 (Cyrillic е)  → e
    """
    cyrillic_apple = "\u0430\u0440\u0440\u04cf\u0435"  # аррӏе
    assert skeleton(cyrillic_apple) == skeleton("apple")


def test_cyrillic_domain_lookalike_in_skeleton():
    """Common Cyrillic-for-Latin domain spoofing patterns all collide."""
    # Cyrillic 'раypal' (ра = р + а, both Cyrillic)
    cyrillic_paypal = "\u0440\u0430ypal"
    # The curated subset maps р→p, а→a, so skeleton folds to 'paypal'
    assert skeleton(cyrillic_paypal) == skeleton("paypal")


# --------------------------------------------------- digit substitutions ---


def test_digit_zero_substitution():
    """g00gle.com should skeleton-match google.com."""
    assert skeleton("g00gle.com") == skeleton("google.com")


def test_digit_three_substitution():
    """Digit 3 → e is a curated override not in UTS #39."""
    assert skeleton("fr3e.com") == skeleton("free.com")


def test_digit_five_substitution():
    """Digit 5 → s is a curated override not in UTS #39."""
    assert skeleton("5ales.com") == skeleton("sales.com")


def test_mixed_digit_letter_substitution():
    """Multiple digit substitutions in one domain."""
    assert skeleton("g00gl3.com") == skeleton("google.com")


# ------------------------------------------------ UTS #39-only coverage ----


def test_uts39_catches_small_capital_o():
    """Small capital O (U+1D0F, ᴏ) is caught by UTS #39 but NOT the
    curated subset. This proves the expanded coverage works.
    """
    scapo = "\u1d0f"  # Latin letter small capital O
    # Not in curated subset
    assert scapo not in _CONFUSABLES_CURATED
    # But IS in the merged map (from UTS #39)
    assert scapo in _CONFUSABLES
    # gᴏᴏgle collides with google
    googlish = f"g{scapo}{scapo}gle"
    assert skeleton(googlish) == skeleton("google")


def test_uts39_catches_small_capital_v():
    """Small capital V (U+1D20, ᴠ) is caught by UTS #39 only."""
    scapv = "\u1d20"
    assert scapv not in _CONFUSABLES_CURATED
    assert scapv in _CONFUSABLES
    # ᴠerizon collides with verizon
    assert skeleton(f"{scapv}erizon") == skeleton("verizon")


def test_uts39_catches_small_capital_z():
    """Small capital Z (U+1D22, ᴢ) is caught by UTS #39 only."""
    scapz = "\u1d22"
    assert scapz not in _CONFUSABLES_CURATED
    assert scapz in _CONFUSABLES
    # ᴢero collides with zero
    assert skeleton(f"{scapz}ero") == skeleton("zero")


# --------------------------------------------- generated module format ----


def test_generated_module_is_importable():
    """The generated module can be imported and has the expected shape."""
    from nodary.confusables_generated import CONFUSABLES_MAP

    assert isinstance(CONFUSABLES_MAP, dict)
    assert len(CONFUSABLES_MAP) > 1000
    # All keys and values are strings.
    for k, v in list(CONFUSABLES_MAP.items())[:100]:
        assert isinstance(k, str)
        assert isinstance(v, str)
    # No empty keys or values.
    assert all(k and v for k, v in CONFUSABLES_MAP.items())
