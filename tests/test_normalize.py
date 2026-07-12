from nodary.feature_extraction.normalize import (
    extract_link_domains,
    is_freemail,
    normalize_address,
    osa_distance,
    reg_domain,
    skeleton,
)


def test_address_normalization():
    assert normalize_address("Jacob+tag@Example.COM") == "jacob@example.com"
    assert normalize_address("j.a.cob+x@gmail.com") == "jacob@gmail.com"
    # dots are significant outside gmail-family domains
    assert normalize_address("j.acob@fastmail.com") == "j.acob@fastmail.com"


def test_reg_domain_uses_public_suffix_list():
    assert reg_domain("mail.example.co.uk") == "example.co.uk"
    assert reg_domain("a.b.example.com") == "example.com"
    assert reg_domain("localhost") == "localhost"


def test_skeleton_folds_homoglyphs():
    assert skeleton("micros0ft.com") == skeleton("microsoft.com")
    assert skeleton("pаypal.com") == skeleton("paypal.com")  # Cyrillic а
    assert skeleton("Acme Corp") == skeleton("acme corp")
    assert skeleton("example.com") != skeleton(
        "exampel.com"
    )  # transposition is NOT a homoglyph


def test_osa_distance():
    assert osa_distance("acme-corp", "acme-corp") == 0
    assert osa_distance("acme-corp", "acme-c0rp") == 1
    assert osa_distance("exampel", "example") == 1  # transposition
    assert osa_distance("abcdef", "uvwxyz", cap=3) == 3


def test_link_extraction_keeps_hostnames_only():
    text = (
        "see https://files.dropbox-share.net/dl/EVIL?token=secret123 and "
        "http://example.com/page also https://sub.example.com/x"
    )
    domains = extract_link_domains(text)
    assert domains == {"dropbox-share.net": 1, "example.com": 2}
    # no URL path, token, or query survives anywhere in the output
    assert all("secret123" not in d and "/" not in d for d in domains)


def test_freemail():
    assert is_freemail("gmail.com")
    assert not is_freemail("myco.com")
