#!/usr/bin/env python3
"""fig5_4914_cdf_tail.py — Fig. 5 at 4.8 GiB in the published figure's style
(bimodal_panels.py: scienceplots, 19 pt, label below each panel, one-row shared
legend), plus an inset in each panel of the complementary CDF (CCDF), 1 - CDF:
the fraction of accesses slower than x, on a log scale.  Each decade down is
one more nine (1e-2 = p99, 1e-3 = p99.9).  The tails, all misses, are visible
there instead of being squashed against 1.0 in the main panel.

(Earlier versions marked each series' mean access latency on its curve.  They
were removed: the text compares mean MISS latencies, which the all-access
markers do not show.)

The x-axis runs to 10 ms; the published figure stopped at 400 us, which cut off
the top few percent of Cylon's 8-thread accesses.

Data: CXDVirt plots/data/fig5_4914/cxdvirt/c4914_d64 (dram_cache_mb=4914,
published eviction regime); Cylon plots/data/fig5_4914/cylon (4,915 MB buffer,
96 GiB profile).  Writes plots/figures/fig5_4914_cdf_tail.{png,pdf}.

    fig5_4914_cdf_tail.py [--data DIR] [--out PATH_WITHOUT_EXTENSION]

Each cell is read from its capture, <cell>.txt, or when that is absent from the
<cell>.lat.gz that scripts/precompute_latencies.py --mio distils it to: the same
integers, sorted, as int32 -- exact for this figure, which only takes quantiles.
"""
import argparse
import gzip
import io
import os

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

try:
    import scienceplots  # noqa: F401
    plt.style.use(["science", "no-latex"])
except Exception:
    pass

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="/mnt/nvme/cxdvirt/cxdvirt/reproduction/plots/data")
ap.add_argument("--out", default="/mnt/nvme/cxdvirt/cxdvirt/reproduction/figures/fig5_4914_cdf_tail",
                help="output path without extension; .png and .pdf are written")
ap.add_argument("--use", action="append", default=[], metavar="EMU:CELL=PATH",
                help="take one cell from another capture, e.g. "
                     "Cylon:8thr_rnd=fig5_4914/cylon_8thr_rnd_run3/8thr_rnd.txt "
                     "(PATH relative to --data; repeatable)")
_a = ap.parse_args()
D, OUT = _a.data, _a.out
USE = {}
for u in _a.use:
    k, _, path = u.partition("=")
    emu, _, cell = k.partition(":")
    USE[(emu, cell)] = path
COL = {"CXDVirt": "#EE7733", "Cylon": "#0077BB"}      # same pair as bimodal_panels.py
LS = {"seq": "-", "rnd": "--"}
SRC = {"CXDVirt": "fig5_4914/cxdvirt/c4914_d64/{t}thr_{p}.txt",
       "Cylon": "fig5_4914/cylon/{t}thr_{p}.txt"}
# WSS = footprint / DRAM cache, the paper's definition (Sec. IV-A):
# 8,390 MiB and 8 x 1,534 MiB over 4,914 MiB.  (1.8x / 2.6x were relative to
# 95% of the cache.)
PANELS = [(1, "1 thread, 1.7$\\times$ WSS"), (8, "8 threads, 2.5$\\times$ WSS")]
FS, LW, W, H, LABEL_PAD = 19, 2.8, 5.5, 3.95, 0.22      # published values, except captions sit closer to the axis title
INSET = [0.53, 0.125, 0.415, 0.37]      # with the larger inset fonts: every label >= 9 px from any curve, >= 8 px from the frame (measured)
INSET_TITLE = "tail latency (CCDF)"
INSET_FS, INSET_TITLE_FS = FS - 4, FS - 3     # tick labels / title
# per-panel inset x-range: at one thread the whole miss tail lies in 3-100 us
# (CXDVirt's miss mode, ~8 us, would sit left of a 10 us start)
INSET_X = {1: ((3e3, 1e5), [1e4, 1e5]), 8: ((1e4, 1e7), [1e4, 1e5, 1e6, 1e7])}
Q = np.concatenate([np.linspace(0, 0.999, 5000), 1 - np.logspace(-3, -6.5, 800)])


def load(path):
    p = f"{D}/{path}"
    if not os.path.exists(p) and os.path.exists(p[:-4] + ".lat.gz"):
        with gzip.open(p[:-4] + ".lat.gz", "rb") as fh:
            return np.load(io.BytesIO(fh.read())).astype(np.float64)
    v = np.array(open(p).read().split(), dtype=np.float64)
    return np.sort(v[np.isfinite(v)])


def _ticks(base, hi):
    """The published ticks, plus decades up to an extended limit."""
    return sorted(set(base) | {10.0 ** k for k in range(4, 12) if base[-1] < 10.0 ** k <= hi})


fig, axes = plt.subplots(1, 2, figsize=(W * 2, H), sharey=True, sharex=True)
x_hi = 1e7          # the published x-range; extended below only if the data need it
for i, (ax, (thr, title)) in enumerate(zip(axes, PANELS)):
    ins = ax.inset_axes(INSET)
    p9999 = 0.0
    for emu in ("CXDVirt", "Cylon"):
        for pat in ("seq", "rnd"):
            v = load(USE.get((emu, f"{thr}thr_{pat}"), SRC[emu].format(t=thr, p=pat)))
            x = np.quantile(v, Q)
            p9999 = max(p9999, float(np.quantile(v, 0.9999)))
            ax.plot(x, Q, lw=LW, color=COL[emu], ls=LS[pat])
            if ins is not None:
                t = Q > 0.5
                ins.plot(x[t], 1 - Q[t], lw=LW * 0.65, color=COL[emu], ls=LS[pat])
    ax.set_xscale("log")
    # The published limits hold every reference curve to P99.99 with margin;
    # a run with longer tails extends them rather than being clipped.
    x_hi = max(x_hi, 1.5 * p9999)
    ax.set_xlim(80, x_hi)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("latency (ns)", fontsize=FS)
    ax.text(0.5, -LABEL_PAD, f"$\\bf{{({chr(ord('a') + i)})}}$ {title}",
            transform=ax.transAxes, ha="center", va="top", fontsize=FS + 3)
    ax.tick_params(labelsize=FS - 2)
    ax.grid(alpha=0.3, lw=0.5, which="both")
    ax.set_axisbelow(True)
    if ins is not None:
        ins.set_xscale("log"); ins.set_yscale("log")
        (ilo, ihi), iticks = INSET_X[thr]
        ihi = max(ihi, 1.5 * p9999)
        ins.set_xlim(ilo, ihi); ins.set_ylim(1e-5, 1)
        ins.set_xticks(_ticks(iticks, ihi)); ins.set_yticks([1e-5, 1e-3, 1e-1])
        ins.tick_params(labelsize=INSET_FS)
        ins.set_title(INSET_TITLE, fontsize=INSET_TITLE_FS, pad=3)
        ins.grid(alpha=0.3, lw=0.4, which="major")
        ins.set_facecolor("white")
axes[0].set_ylabel("CDF", fontsize=FS)
fig.subplots_adjust(wspace=0.06)

h = [Line2D([], [], color=COL["CXDVirt"], lw=LW, ls="-", label="CXDVirt, sequential"),
     Line2D([], [], color=COL["CXDVirt"], lw=LW, ls="--", label="CXDVirt, random"),
     Line2D([], [], color=COL["Cylon"], lw=LW, ls="-", label="Cylon, sequential"),
     Line2D([], [], color=COL["Cylon"], lw=LW, ls="--", label="Cylon, random")]
# one row, as bimodal_panels.py --shared-legend draws it (anchored a little higher:
# at 1.00 it overlapped the panels by 5 px in this layout).  Spacing is tightened and
# x=0.49 centres it on the panels (the CDF label shifts them left) so the legend
# stays within their width instead of widening the figure (measured).
fig.legend(handles=h, loc="upper center", ncol=len(h), fontsize=FS - 2, framealpha=0.0,
           bbox_to_anchor=(0.49, 1.045), handlelength=1.6, columnspacing=0.6, handletextpad=0.4)
fig.savefig(OUT + ".png", bbox_inches="tight", dpi=200)
fig.savefig(OUT + ".pdf", bbox_inches="tight")
print(f"wrote {OUT}.png\n      {OUT}.pdf")
