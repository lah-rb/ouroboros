"""HOM glyph repair: mangled CM-font sheets are fixed, clean sheets untouched."""

import os

from hom_text import HOM_DIR, _collapse_layout, iter_sheets, repair_glyphs

MANGLED = (
    "Quartz                          SiO2\n"
    "                                °\n"
    "                                c 2001 Mineral Data Publishing, ver sion 1.2\n\n"
    "Crystal Data: Hexagonal. Point Group: 32: As enantimorphic prismatic crystals, with\n"
    'f1010g terminated by f1011g, striated ? [0001]; may be °attened, rarely \\twisted."\n'
    "penetration twins on the Dauphin¶ e law; drusy, ¯ne-grained. From Bourg d'Oisans, Isµere.\n"
    "Physical Properties: Hardness = 7. D(meas.) = 2.65; 2.59{2.63 when massive.\n"
    "Optical Class: Uniaxial (+). ! = 1.544 ² = 1.553\n"
    "Polymorphism & Series: stable below 573 ± C. Association: Calcite, °uorite. Je®erson Co.\n"
    "References: (1) Frondel, C. (1962) Dana's system, 9{250.\n"
)


def test_repair_rules():
    t = repair_glyphs(MANGLED)
    for good in (
        "{1010}",
        "{1011}",
        "⊥ [0001]",
        "flattened",
        "\u201ctwisted.\u201d",
        "Dauphiné",
        "fine-grained",
        "Isère",
        "2.59–2.63",
        "ω = 1.544",
        "ε = 1.553",
        "573 °C",
        "fluorite",
        "Jefferson",
        "9–250",
        "version 1.2",
    ):
        assert good in t, good
    for bad in ("f1010g", "°attened", "¯ne", "Je®erson", "¶", "µe", "2.59{2.63", "± C"):
        assert bad not in t, bad


def test_clean_unicode_sheet_passes_unchanged():
    clean = (
        "Anatase   TiO2\nCrystal Data: Tetragonal. Crystals typically acute dipyramidal {011}, to 3.75 cm.\n"
        "Hardness = 5.5–6 VHN = 616–698 (100 g load). D(meas.) = 3.79–3.97\n"
        "ω = 2.5612 ε = 2.4880. stable below 573 °C. From Bourg d’Oisans, Isère, France.\n"
    )
    assert repair_glyphs(clean) == clean


def test_furniture_dropped_and_sections_paragraphed():
    t = _collapse_layout(repair_glyphs(MANGLED))
    assert "Mineral Data Publishing" not in t
    assert (
        "\nCrystal Data:" in t
        and "\n\nPhysical Properties:" in t
        and "\n\nReferences:" in t
    )
    assert "  " not in t


def test_iter_sheets_reads_the_mirror_if_present():
    if not os.path.isdir(HOM_DIR):
        return
    rows = list(iter_sheets(limit=2))
    assert rows and all("Crystal Data:" in tx for _, tx, _ in rows)
