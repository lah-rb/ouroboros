"""Held-out species selection: balance, determinism, and what it refuses."""

from holdout import (
    AMBIGUOUS_NAMES,
    BAND_HOLDOUT,
    PROTECTED,
    complexity,
    recurrence_band,
    select_holdout,
)

_FORMULAS = {
    "Quartz": "SiO2",
    "Albite": "NaAlSi3O8",
    "Calcite": "CaCO3",
    "Azurite": "Cu2+3(CO3)2(OH)2",
    "Antigorite": "Mg3Si2O5(OH)4",
    "Actinolite": "Ca2(Mg,Fe2+)5Si8O22(OH)2",
    "Palladium": "Pd",
    "Copper": "Cu",
    "Halite": "NaCl",
    "Jarosite": "KFe3+3(SO4)2(OH)6",
    "Brookite": "TiO2",
    "Galena": "PbS",
    "Cuprite": "Cu2O",
    "Niter": "KNO3",
    "Gallite": "CuGaS2",
}


def _papers(n):
    return [f"doi_p{i}" for i in range(n)]


# ── complexity axis ──────────────────────────────────────────────────
def test_simple_and_complex_tiers_separate_real_cases():
    assert complexity("SiO2")["tier"] == "simple"
    assert complexity("CaCO3")["tier"] == "simple"
    assert complexity("(Mg,Fe+2)3Si2O5(OH)4")["tier"] == "complex"
    assert complexity("Ca2(Mg,Fe2+)5Si8O22(OH)2")["tier"] == "complex"


def test_hydration_and_solid_solution_raise_the_tier():
    """Both make identification harder: water crowds the mid-IR, and a
    solid solution has no single reference spectrum."""
    plain = complexity("KAl2Si3O10")
    solution = complexity("(K,Na)Al2Si3O10")
    assert solution["solid_solution"] and not plain["solid_solution"]
    assert complexity("KAl2(AlSi3O10)(OH)2")["hydrated"]


def test_element_parse_prefers_longer_symbols():
    els = complexity("CaSiO3")["elements"]
    assert "Si" in els and "S" not in els


# ── recurrence bands ─────────────────────────────────────────────────
def test_bands_cover_the_whole_range():
    assert recurrence_band(36) == "head"
    assert recurrence_band(12) == "common"
    assert recurrence_band(5) == "mid"
    assert recurrence_band(2) == "rare"
    assert recurrence_band(1) == "singleton"
    assert recurrence_band(0) == "absent"


# ── selection ────────────────────────────────────────────────────────
def test_protected_species_are_never_held_out():
    sp = {"Quartz": _papers(40), "Albite": _papers(36), "Azurite": _papers(26)}
    out = select_holdout(sp, _FORMULAS)
    assert "Quartz" not in {s for picks in out.values() for s in picks}


def test_native_element_minerals_are_not_candidates():
    """ "Palladium"/"Copper" name a species AND the everyday material, so
    a corpus mention is as likely to be a catalyst as a mineral."""
    sp = {"Palladium": _papers(5), "Copper": _papers(5), "Cuprite": _papers(5)}
    picked = {s for picks in select_holdout(sp, _FORMULAS).values() for s in picks}
    assert "Palladium" not in picked and "Copper" not in picked
    assert "Cuprite" in picked


def test_holdout_balances_simple_against_complex():
    sp = {
        "Halite": _papers(5),
        "Brookite": _papers(5),
        "Galena": _papers(5),
        "Niter": _papers(5),
        "Cuprite": _papers(5),
        "Gallite": _papers(5),
        "Azurite": _papers(5),
        "Jarosite": _papers(5),
        "Antigorite": _papers(5),
        "Actinolite": _papers(5),
    }
    picks = select_holdout(sp, _FORMULAS)["mid"]
    tiers = [complexity(_FORMULAS[s])["tier"] for s in picks]
    assert tiers.count("simple") == tiers.count("complex")


def test_selection_is_deterministic():
    sp = {k: _papers(5) for k in _FORMULAS if k != "Quartz"}
    assert select_holdout(sp, _FORMULAS) == select_holdout(sp, _FORMULAS)


def test_selection_is_not_alphabetically_clustered():
    """Regression: taking the head of a sorted pool drew an all-A/B
    holdout, and mineral names cluster by naming tradition and type
    locality, so the alphabet's head is a biased sample."""
    sp = {f"Mineral{c}ite": _papers(5) for c in "ABCDEFGHIJKLMNOP"}
    forms = {
        k: ("SiO2" if i % 2 else "Ca2(Mg,Fe2+)5Si8O22(OH)2") for i, k in enumerate(sp)
    }
    picks = select_holdout(sp, forms)["mid"]
    assert len({s[7] for s in picks}) > 1


def test_band_sizes_are_respected():
    sp = {k: _papers(1) for k in _FORMULAS if k not in PROTECTED}
    picks = select_holdout(sp, _FORMULAS)["singleton"]
    assert len(picks) <= BAND_HOLDOUT["singleton"]


def test_ambiguous_list_and_native_rule_do_not_contradict():
    for name in ("copper", "silicon", "graphite"):
        assert name in AMBIGUOUS_NAMES
