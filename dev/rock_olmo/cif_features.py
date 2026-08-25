"""AMCSD CIF -> coordination and bond-length features.

WHY THIS AND NOT MORE METADATA. mindat's structural block (crystal
system, cell, Strunz) gave +0.071 holdout but did NOT move polymorphs:
it is metadata ABOUT a structure, not the structure. Raman frequency is
set by bonding geometry -- roughly sqrt(force constant / reduced mass) --
so the quantities that should actually predict band positions are BOND
LENGTHS and COORDINATION NUMBERS, and those require atomic positions.
AMCSD carries them for 2,349 named minerals.

The physics worth encoding, in order of how directly it maps to a band:
  * shortest cation-anion bond -- stiffest bond, highest-frequency mode.
    Si-O near 1.62 A is why silicates band around 1000 cm-1.
  * coordination number -- the same cation in 4- vs 6-fold coordination
    gives a different force constant and a different band.
  * spread of bond lengths in a polyhedron -- distortion, which splits
    otherwise degenerate modes.
  * reduced mass of the bonded pair -- the other half of the frequency.

WHY PYMATGEN. Symmetry expansion from the asymmetric unit is where a
hand-rolled parser silently goes wrong: a missed symop yields too few
atoms and every coordination number comes out low, with no error raised.
Operator ruling: use a library rather than chance it.

NEIGHBOUR FINDING: CrystalNN, after a covalent-radius cutoff was tried
first and failed. The cutoff was assumed too slow to replace; it is not
-- CrystalNN costs 6-46 ms per structure, ~4 minutes for the whole set.
It was also WRONG in a way the first validation could not see: a
tolerance tuned on Si-O undercounted large, soft, lone-pair cations
(12% of Pb sites came out below CN 2, and "Pb in 1.38-fold coordination"
reached the emitted corpus), while widening it overshot the other way
(Ba 18 against a textbook 12). One radius multiplier cannot serve both
Si-O at 1.6 A and Ba-O at 2.8 A. CrystalNN is a Voronoi method with
solid-angle weighting and needs no such constant.

VALIDATION IS THE POINT. Coordination numbers are checkable against
textbook values, so a wrong extractor gets caught rather than trusted --
but only if the validation set spans the failure modes. The original six
minerals were all small hard cations and passed 6/6 while a whole class
was silently wrong. Galena (Pb) and Barite (Ba) are in the set for that
reason.
"""

from __future__ import annotations

import glob
import json
import os
import re
import statistics
import sys
import warnings

warnings.filterwarnings("ignore")

AMCSD = os.path.expanduser("~/corpora/mineral-refs/amcsd")
OUT = os.path.expanduser("~/corpora/rock-olmo-training/cif_features.json")

# Anions we measure bonds TO. Raman in our 100-1200 cm-1 window is
# dominated by cation-anion stretching and bending in these frameworks.
ANIONS = {"O", "S", "F", "Cl", "OH", "Se", "Te", "N", "Br", "I", "As"}

#: Hydrogen is genuinely 1-coordinate in a hydroxyl, so it is not an
#: extraction error — but "H in 1-fold coordination" is not a statement
#: anyone makes about a structure, and it dominated the coordination
#: dict (576 of 584 H sites). Bonds to H are still measured; only the
#: coordination REPORT excludes it.
NO_COORD_REPORT = {"H"}

#: Below this a cation coordination number is not a measurement, it is a
#: missed bond. Lone-pair cations (Pb2+, Sb3+, Tl+, Bi3+) sit in very
#: asymmetric sites with several long bonds that a covalent-radius cutoff
#: tuned on Si-O simply does not reach — 12% of Pb sites came out under 2.
#: Retry those wider, and if they are still implausible OMIT them rather
#: than assert a number we do not believe.
MIN_PLAUSIBLE_CN = 2.0


def _site_symbol(site) -> str | None:
    """Element at a site, tolerating PARTIAL OCCUPANCY.

    Solid solutions (olivine Mg/Fe, plagioclase Na/Ca) are stored as one
    site with fractional occupants, and `site.specie` RAISES on those
    rather than returning anything -- which crashed the first run on
    Forsterite. Take the dominant occupant: coordination is a property of
    the site, and the majority cation is the one the geometry belongs to."""
    try:
        return site.specie.symbol
    except AttributeError:
        pass
    try:
        sp, _ = max(site.species.items(), key=lambda kv: kv[1])
        return getattr(sp, "symbol", None) or str(sp)
    except (AttributeError, ValueError):
        return None


def _name_of(path: str) -> str | None:
    m = re.search(
        r"^_chemical_name_mineral\s+(.+)$", open(path, errors="ignore").read(), re.M
    )
    if not m:
        return None
    n = m.group(1).strip().strip("'\"")
    return n or None


_NN = None


def _crystal_nn():
    """One CrystalNN instance for the whole run — constructing it per
    structure dominated the cost when it was first tried."""
    global _NN
    if _NN is None:
        from pymatgen.analysis.local_env import CrystalNN

        _NN = CrystalNN()
    return _NN


def _ordered(st):
    """An ordered copy, each site taking its dominant occupant.

    CrystalNN calls `site.specie` internally, which RAISES on partial
    occupancy — and 149 of 186 AMCSD structures are disordered, because
    solid solution is the norm for minerals rather than the exception.
    Switching to CrystalNN without this dropped the yield from 2,152
    minerals to ~1,244 while the failures looked like ordinary parse
    errors. Geometry is what we measure, so collapsing each site to its
    majority cation preserves exactly what the coordination analysis
    needs and discards only the occupancy fraction."""
    from pymatgen.core import Structure

    if st.is_ordered:
        return st
    species = []
    for site in st:
        sp, _ = max(site.species.items(), key=lambda kv: kv[1])
        species.append(sp)
    return Structure(st.lattice, species, st.frac_coords)


def features_for(path: str) -> dict | None:
    from pymatgen.core import Structure

    try:
        st = _ordered(Structure.from_file(path))
    except Exception as e:
        if os.environ.get("CIF_DEBUG"):
            print(f"    from_file failed {os.path.basename(path)}: {e}", flush=True)
        return None
    if len(st) == 0 or len(st) > 400:
        return None

    cn: dict[str, list[int]] = {}
    blen: dict[str, list[float]] = {}
    shortest = None
    nn = _crystal_nn()
    for i, site in enumerate(st):
        s1 = _site_symbol(site)
        if s1 is None or s1 in ANIONS:
            continue
        try:
            info = nn.get_nn_info(st, i)
        except Exception:
            continue
        bonded = []
        for entry in info:
            nb = entry.get("site")
            s2 = _site_symbol(nb) if nb is not None else None
            if s2 is None or s2 not in ANIONS:
                continue
            try:
                d = float(site.distance(nb))
            except Exception:
                continue
            bonded.append((d, s2))
        if not bonded:
            continue
        cn.setdefault(s1, []).append(len(bonded))
        for d, s2 in bonded:
            blen.setdefault(f"{s1}-{s2}", []).append(d)
            if shortest is None or d < shortest[0]:
                shortest = (d, f"{s1}-{s2}")

    if not cn:
        return None
    # Drop what we still do not believe rather than publishing it. An
    # omitted coordination number costs one clause of one sentence; a
    # wrong one ("Pb in 1.38-fold coordination") is a fabricated fact in
    # grounded-looking prose.
    cn = {
        el: v
        for el, v in cn.items()
        if el not in NO_COORD_REPORT and (sum(v) / len(v)) >= MIN_PLAUSIBLE_CN
    }
    if not cn:
        return None
    pair_stats = {
        k: {
            "mean": round(statistics.mean(v), 4),
            "min": round(min(v), 4),
            "spread": round(max(v) - min(v), 4),
            "n": len(v),
        }
        for k, v in sorted(blen.items())
    }
    # The TRUE space group, which mindat does not supply (its `spacegroup`
    # column is an internal id). Taken from the symmetry of the parsed
    # structure rather than the CIF header string, so it is consistent even
    # where the header is malformed.
    try:
        sg_sym, sg_no = st.get_space_group_info()
    except Exception:
        sg_sym, sg_no = None, None
    return {
        "space_group_symbol": sg_sym,
        "space_group_number": sg_no,
        "n_sites": len(st),
        "volume_per_atom": round(st.volume / len(st), 4),
        "density": round(float(st.density), 4),
        "coordination": {
            k: round(statistics.mean(v), 3) for k, v in sorted(cn.items())
        },
        "bonds": pair_stats,
        "shortest_bond_a": round(shortest[0], 4) if shortest else None,
        "shortest_bond_pair": shortest[1] if shortest else None,
    }


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--validate":
        # Galena and Barite were added after the first extraction shipped
        # "Pb in 1.38-fold coordination" into the corpus: the original six
        # were all small, hard cations (Si, Fe, Ti, Ca, Mg, C) and passed
        # 6/6 while a whole class — large, soft, lone-pair cations — was
        # silently undercounted. A validation set that cannot fail on the
        # case you got wrong is not validating anything.
        want = {
            "Diopside": {"Si": 4, "Mg": 6, "Ca": 8},
            "Quartz": {"Si": 4},
            "Pyrite": {"Fe": 6},
            "Rutile": {"Ti": 6},
            "Forsterite": {"Si": 4, "Mg": 6},
            "Calcite": {"Ca": 6, "C": 3},
            "Galena": {"Pb": 6},
            "Barite": {"Ba": 12},
        }
        # ALL candidate CIFs per name, not the first: AMCSD holds several
        # structures per mineral and some are unparseable stubs. The first
        # Rutile file is "Invalid CIF file with no structures", which the
        # first validation reported as a Rutile failure rather than a file
        # failure. main() already scans every file, so validating on one
        # was testing something the real run does not do.
        idx: dict[str, list[str]] = {}
        for p in sorted(glob.glob(os.path.join(AMCSD, "*.cif"))):
            n = _name_of(p)
            if n and n in want:
                idx.setdefault(n, []).append(p)
        print("VALIDATION — coordination numbers against textbook values\n")
        for n, exp in want.items():
            if n not in idx:
                print(f"  {n:12s} not in AMCSD")
                continue
            f = None
            for cand in idx[n]:
                f = features_for(cand)
                if f:
                    break
            if not f:
                print(f"  {n:12s} PARSE FAILED ({len(idx[n])} CIFs all unusable)")
                continue
            got = f["coordination"]
            ok = all(abs(got.get(e, -9) - v) <= 0.51 for e, v in exp.items())
            print(
                f"  {n:12s} {'PASS' if ok else 'FAIL'}  got "
                f"{ {k: got.get(k) for k in exp} }  want {exp}   "
                f"shortest {f['shortest_bond_pair']} {f['shortest_bond_a']} A"
            )
        return

    files = sorted(glob.glob(os.path.join(AMCSD, "*.cif")))
    best: dict[str, dict] = {}
    done = failed = 0
    for i, p in enumerate(files, 1):
        n = _name_of(p)
        if not n:
            continue
        f = features_for(p)
        if not f:
            failed += 1
            continue
        done += 1
        # Keep the structure with the MOST bonded sites resolved: partial
        # occupancy and stripped hydrogens make some CIFs of a mineral much
        # thinner than others, and the thin one would understate coordination.
        prev = best.get(n)
        if prev is None or len(f["bonds"]) > len(prev["bonds"]):
            f["source_cif"] = os.path.basename(p)
            best[n] = f
        if i % 1000 == 0:
            print(
                f"  {i}/{len(files)} scanned, {len(best)} minerals, "
                f"{failed} unparseable",
                flush=True,
            )
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(best, open(OUT, "w"), indent=1, sort_keys=True)
    print(f"\n{len(best)} minerals -> {OUT}  ({done} CIFs used, {failed} failed)")


if __name__ == "__main__":
    main()
