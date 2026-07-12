"""Deterministic normalization: addresses, registrable domains, skeletons.

All rules are versioned constants — changing any of them bumps
NORMALIZE_VERSION, which invalidates derived profiles (rebuild required).
No network access: the Public Suffix List is tldextract's bundled snapshot.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

NORMALIZE_VERSION = 1

# Domains whose reputation must never propagate to unrelated senders
# (anyone can register a mailbox there). Vendored, versioned list.
FREEMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "ymail.com",
        "rocketmail.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "proton.me",
        "protonmail.com",
        "pm.me",
        "tutanota.com",
        "tuta.io",
        "gmx.com",
        "gmx.net",
        "gmx.de",
        "web.de",
        "mail.com",
        "mail.ru",
        "yandex.com",
        "yandex.ru",
        "zoho.com",
        "fastmail.com",
        "fastmail.fm",
        "hey.com",
        "qq.com",
        "163.com",
        "126.com",
        "sina.com",
        "naver.com",
        "daum.net",
        "hushmail.com",
        "mailfence.com",
        "posteo.de",
        "runbox.com",
        "comcast.net",
        "verizon.net",
        "att.net",
        "sbcglobal.net",
        "cox.net",
        "charter.net",
        "earthlink.net",
        "juno.com",
        "optonline.net",
        "btinternet.com",
        "sky.com",
        "talktalk.net",
        "virginmedia.com",
        "orange.fr",
        "wanadoo.fr",
        "free.fr",
        "sfr.fr",
        "laposte.net",
        "t-online.de",
        "freenet.de",
        "libero.it",
        "virgilio.it",
        "tiscali.it",
        "seznam.cz",
        "wp.pl",
        "onet.pl",
        "o2.pl",
        "interia.pl",
        "rediffmail.com",
        "duck.com",
        "duckduckgo.com",
        "simplelogin.io",
        "anonaddy.me",
    }
)

# Providers where dots in the local part are insignificant.
_DOT_INSENSITIVE = frozenset({"gmail.com", "googlemail.com"})

# Homoglyph folding table (subset of Unicode UTS #39 confusables, plus the
# digit substitutions actually seen in lookalike domains). Applied after NFKD
# decomposition strips diacritics. Deliberately small and auditable.
_CONFUSABLES = {
    # Cyrillic -> Latin
    "а": "a",
    "е": "e",
    "о": "o",
    "р": "p",
    "с": "c",
    "х": "x",
    "у": "y",
    "і": "i",
    "ѕ": "s",
    "ј": "j",
    "ԁ": "d",
    "ɡ": "g",
    "һ": "h",
    "к": "k",
    "м": "m",
    "т": "t",
    "в": "b",
    "н": "h",
    "ѡ": "w",
    "ѵ": "v",
    "ꞅ": "s",
    # Greek -> Latin
    "α": "a",
    "β": "b",
    "ε": "e",
    "η": "n",
    "ι": "i",
    "κ": "k",
    "ν": "v",
    "ο": "o",
    "ρ": "p",
    "τ": "t",
    "υ": "u",
    "ω": "w",
    # Digits / symbols commonly used as letters
    "0": "o",
    "1": "l",
    "3": "e",
    "5": "s",
    "ø": "o",
    "ł": "l",
    "đ": "d",
    # Latin lookalikes
    "ı": "i",
    "ǀ": "l",
    "ⅼ": "l",
    "ⅰ": "i",
    "ⅴ": "v",
}

_WS_RE = re.compile(r"\s+")
_URL_HOST_RE = re.compile(
    r"""https?://(?:[^\s/@"'<>]*@)?([^\s/:?#"'<>\)\]]+)""", re.IGNORECASE
)
_EMAIL_IN_TEXT_RE = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")


@lru_cache(maxsize=1)
def _extractor():
    import tldextract

    # Empty suffix_list_urls => bundled snapshot only, never a network fetch.
    return tldextract.TLDExtract(suffix_list_urls=())


def reg_domain(host: str) -> str:
    """Registrable domain per the (vendored) Public Suffix List.

    'mail.example.co.uk' -> 'example.co.uk'. Falls back to the raw host when
    no suffix matches (e.g. 'localhost', bare IPs).
    """
    host = host.strip().strip(".").lower()
    ext = _extractor()(host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return host


def skeleton(text: str) -> str:
    """Homoglyph-folded form. Two strings that render alike collide here."""
    out = []
    for ch in unicodedata.normalize("NFKD", text.lower()):
        if unicodedata.combining(ch):
            continue
        out.append(_CONFUSABLES.get(ch, ch))
    return "".join(out)


def normalize_address(addr: str) -> str:
    """Casefold, strip +tag; strip local-part dots for dot-insensitive providers."""
    addr = addr.strip().strip("<>").lower()
    if "@" not in addr:
        return addr
    local, _, domain = addr.rpartition("@")
    domain = domain.strip(".")
    local = local.split("+", 1)[0]
    if reg_domain(domain) in _DOT_INSENSITIVE or domain in _DOT_INSENSITIVE:
        local = local.replace(".", "")
    return f"{local}@{domain}"


def address_domain(addr_norm: str) -> str:
    return addr_norm.rpartition("@")[2]


def is_freemail(rdomain: str) -> bool:
    return rdomain in FREEMAIL_DOMAINS


def normalize_display_name(name: str) -> str:
    return _WS_RE.sub(" ", name.strip().strip('"').strip()).casefold()


def extract_link_domains(text: str) -> dict[str, int]:
    """Registrable domains of http(s) URLs in text. Hostnames only —
    paths, queries, and the surrounding text are never retained."""
    counts: dict[str, int] = {}
    for m in _URL_HOST_RE.finditer(text):
        host = m.group(1).rsplit(":", 1)[0] if ":" in m.group(1) else m.group(1)
        rd = reg_domain(host)
        if "." in rd or rd:  # keep bare hosts too (e.g. IP literals)
            counts[rd] = counts.get(rd, 0) + 1
    return counts


def emails_in_text(text: str) -> list[str]:
    """Email-like tokens found in a display name (used for the
    embedded-address-mismatch feature)."""
    return [m.group(0).lower() for m in _EMAIL_IN_TEXT_RE.finditer(text)]


def osa_distance(a: str, b: str, cap: int = 3) -> int:
    """Optimal-string-alignment (restricted Damerau-Levenshtein) distance,
    early-exiting at `cap` since we only care about small distances."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and ca == b[j - 2] and cb == a[i - 2]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) >= cap:
            return cap
        prev2, prev = prev, cur
    return min(prev[len(b)], cap)
