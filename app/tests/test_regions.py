import pytest

from app.regions import region_from_note, region_from_offer_name, resolve_region


@pytest.mark.parametrize("name, region", [
    ("1 Month (UK) Standard", "UK"),
    ("1 Month (US) Pro", "US"),
    ("1 Month (eu) Pro", "EU"),
    ("Bundle (Global)", "Global"),
    ("(iOS) 5 Rainbow Cards", None),   # платформа, не регион
    ("Weekly Pass", None),
    ("ID Card Pack", None),            # «ID» без скобок — не регион
])
def test_region_from_offer_name(name, region):
    assert region_from_offer_name(name) == region


@pytest.mark.parametrize("note, region", [
    ("Region: Global\nPUBG Mobile top-up.", "Global"),
    ("Region: India\nCODM", "IN"),
    ("Region: Kazakhstan", "KZ"),
    ("Region: United States", "US"),
    ("Region: Russia\nMagic Chess", "RU"),
    ("Region: Germany", "DE"),          # своя страна, а не общий «Europe»
    ("Region: Europe", "EU"),
    ("Region: CIS", "CIS"),
    ("Region: MENA", "MENA"),
    ("Region: CN / Other / US / VN", None),
    ("8 Ball Pool top-up.", None),
    (None, None),
])
def test_region_from_note(note, region):
    assert region_from_note(note) == region


def test_resolve_region_priority():
    note = "Region: India"
    assert resolve_region("1 Month (UK) Standard", "US", note) == "UK"  # номинал важнее всего
    assert resolve_region("60 UC", "us", note) == "US"                  # затем конфиг
    assert resolve_region("60 UC", None, note) == "IN"                  # затем примечание FZ
    assert resolve_region("60 UC", None, "top-up") == "Global"          # иначе Global
    assert resolve_region("60 UC", "global", None) == "Global"
