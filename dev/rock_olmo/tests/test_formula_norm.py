"""Every convention the 2026-09-10 census found, pinned to the textbook target."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from formula_norm import normalize_formula, normalize_text_formulas  # noqa: E402

MAGNETITE = [
    "Fe3O4",
    "Fe^2+^Fe^3+^2O4",  # RRUFF
    "Fe<sup>2+</sup>Fe<sup>3+</sup><sub>2</sub>O<sub>4</sub>",  # mindat
    "Fe3 O4",  # ROD / CIF
    "Fe₃O₄",  # unicode
    "Fe_{3}O_{4}",  # paper markdown LaTeX
    "Fe$_{3}$O$_{4}$",
    "Fe_3O_4",
    "Fe 3 O 4",  # OCR
]


def test_magnetite_all_conventions_converge():
    # the mixed-valence spellings are a separate fact; every ONE-formula spelling must agree
    for s in MAGNETITE:
        assert normalize_formula(s) in ("Fe3O4", "FeFe2O4"), (s, normalize_formula(s))
    assert normalize_formula("Fe++Fe+++2O4") == "FeFe2O4"  # webmineral: charges dropped, structure kept


def test_calcite_grouping_and_cif_order():
    assert normalize_formula("Ca(CO3)") == "CaCO3"
    assert normalize_formula("CaCO<sub>3</sub>") == "CaCO3"
    assert normalize_formula("C Ca O3") == "CaCO3"
    assert normalize_formula("CaCO_{3}") == "CaCO3"


def test_quartz_and_cif_sums():
    assert normalize_formula("O2 Si") == "SiO2"
    assert normalize_formula("SiO₂") == "SiO2"
    assert normalize_formula("F4 Li Y") == "LiYF4"
    assert normalize_formula("C31 H32 N4 Ni") == "NiC31H32N4"  # abelsonite, RRUFF's order


def test_hydration_one_dot_no_spaces():
    for s in ("CaSO4·2H2O", "CaSO4•2H2O", "CaSO4.2H2O", "CaSO4 · 2H2O", "Ca(SO4)·2H2O", "CaSO4·2(H2O)"):
        assert normalize_formula(s) == "CaSO4·2H2O", (s, normalize_formula(s))
    assert normalize_formula("K(UO2)(AsO4)•4(H2O)") == "K(UO2)(AsO4)·4H2O"
    assert normalize_formula("Al2O3•(SiO2)1.3-2•((H2O))2.5-3") == "Al2O3·(SiO2)1.3-2·(H2O)2.5-3"


def test_vacancy_charges_ranges_dropped_or_kept_as_ascii():
    assert normalize_formula("[box]Ca2(Mg4.5-2.5Fe^2+^0.5-2.5)Si8O22(OH)2") == "Ca2(Mg4.5-2.5Fe0.5-2.5)Si8O22(OH)2"
    assert normalize_formula("&#9723;Ca<sub>2</sub>(Mg<sub>4.5-2.5</sub>Fe<sup>2+</sup><sub>0.5-2.5</sub>)Si<sub>8</sub>O<sub>22</sub>(OH)<sub>2</sub>") == "Ca2(Mg4.5-2.5Fe0.5-2.5)Si8O22(OH)2"
    assert normalize_formula("Ca2(Mg,Fe++)5Si8O22(OH)2") == "Ca2(Mg,Fe)5Si8O22(OH)2"
    assert normalize_formula("Ni++C31H32N4") == "NiC31H32N4"
    assert normalize_formula("Ag1-xSbx(x=0.009-0.16)") == "Ag1-xSbx(x=0.009-0.16)"
    assert normalize_formula("(ZnO)ₓ(TiO₂)₁₋ₓ") == "(ZnO)x(TiO2)1-x"


def test_groups_with_multipliers_stay():
    assert normalize_formula("Ca5(PO4)3(OH)") == "Ca5(PO4)3(OH)"
    assert normalize_formula("Ca<sub>5</sub>(PO<sub>4</sub>)<sub>3</sub>(OH)") == "Ca5(PO4)3(OH)"
    assert normalize_formula("NaPb2(CO3)2(OH)") == "NaPb2(CO3)2(OH)"
    assert normalize_formula("(Mg,Fe)2SiO4") == "(Mg,Fe)2SiO4"


def test_greek_prefix_and_dashes():
    assert normalize_formula("α–Fe2 O3") == "α-Fe2O3"


def test_prose_pass_rewrites_formulas_and_leaves_math():
    t = "Magnetite (Fe_{3}O_{4}) and hematite (α-Fe₂O₃) nanoparticles; SiO$_{2}$ substrate, TiO_2} films."
    out = normalize_text_formulas(t)
    assert "Fe3O4" in out and "α-Fe2O3" in out and "SiO2" in out and "TiO2" in out, out
    assert "_{" not in out and "₂" not in out
    math = "the energy $E = mc^2$ and index x_{i} with T_{c} = 4 K"
    assert normalize_text_formulas(math) == math
    ions = "Fe<sup>3+</sup> and Fe³⁺ and Fe^{3+} substitute for Al<sup>3+</sup>"
    out2 = normalize_text_formulas(ions)
    assert out2.count("Fe3+") == 3 and "Al3+" in out2, out2
    assert normalize_text_formulas("the Fe 3 O 4 phase") == "the Fe3O4 phase"
