"""webmineral page -> folded Label: value prose; furniture cut."""

import os

from webmineral_text import WM_DIR, iter_pages, render

PAGE = """<html><head><title>Abelsonite Mineral Data</title><style>x{}</style></head><body>
<a>Home</a><a>Crystal</a><a>Help</a><a>About</a>
<h2><b>General Abelsonite  Information </b></h2>
<td>Chemical Formula:</td><td>Ni++C31H32N4</td>
<td>Empirical Formula:</td><td>NiC<sub>31</sub>H<sub>32</sub>N<sub>4</sub></td>
<td>Environment:</td><td>Fractures in lacustrine, kerogen-rich shales.</td>
<td>Name Origin:</td><td>Named after Philip H. Abelson.</td>
<h3>Abelsonite Image</h3><td>Images:</td><td>Abelsonite</td><td>Comments:</td><td>Purplish red crystal fragment.</td><td>&copy;</td><td>Thomas Witzke</td>
<h2><b>Abelsonite  Crystallography</b></h2>
<td>Crystal System:</td><td>Triclinic</td><td>Cell Dimensions:</td><td>a = 8.44, b = 11.12, c = 7.28</td>
<h2><b>Physical Properties of Abelsonite </b></h2>
<td>Hardness:</td><td>2-2.5 - Gypsum-Finger Nail</td><td>Habit:</td><td>Platy</td><td>Habit:</td><td>Aggregates</td>
<h2><b>Other Abelsonite  Information</b></h2>
<td>References:</td><td>NAME( Dana8) PHYS. PROP.(Enc. of Minerals,2nd ed.,1990)</td>
<td>See Also:</td><td>Links to other databases</td><td>1 -</td><td>Athena</td>
<td>Search for Abelsonite using:</td><td>Google</td>
</body></html>"""


def test_render_folds_labels_and_cuts_furniture():
    species, text = render(PAGE)
    assert species == "Abelsonite"
    assert text.startswith("## General Information")
    assert "Chemical Formula: Ni++C31H32N4" in text
    assert "Empirical Formula: NiC31H32N4" in text  # subscripts re-fused
    assert "Comments: Purplish red crystal fragment." in text
    assert "Thomas Witzke" not in text and "Images:" not in text
    assert (
        "## Crystallography" in text
        and "Cell Dimensions: a = 8.44, b = 11.12, c = 7.28" in text
    )
    assert text.count("Habit:") == 2
    assert "References: NAME( Dana8)" in text
    assert "See Also" not in text and "Athena" not in text and "Google" not in text
    assert "Home" not in text  # nav chrome before the first section


def test_iter_pages_reads_the_mirror_if_present():
    if not os.path.isdir(WM_DIR):
        return
    rows = list(iter_pages(limit=3))
    assert rows and all(sp and "## General Information" in tx for sp, tx, _ in rows)
