"""Turn published spectrum plots into peak sets.

A plotted spectrum is the universal intake form for this data — papers that
dump raw spectra are rare, papers that print a figure are everywhere. Read as
a single-pass vision consumable, a plot loses an unbounded amount of what it
carries. Read as a MATRIX, the loss concentrates in the data-to-pixels
rasterisation step, which is bounded and measurable, plus an axis calibration
term that is bounded only by refusal.

The core (source/axes/curve/peaks) is technique-agnostic and must never name a
unit; a thin adapter maps positions to a reference catalogue. LIBS ships
first, XRD and Raman drop in beside it.
"""
