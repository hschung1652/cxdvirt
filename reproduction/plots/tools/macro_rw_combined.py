#!/usr/bin/env python3
"""Write- and read-intensive macrobenchmarks side by side: CXDVirt vs Cylon.

(a) left  -- Splash-4 OCEAN execution time, stacked init + solve, 8 THREADS.
              Ocean dirties its whole working set every timestep, so it drives
              the allocate and write-back paths Redis never touches.  Dashed
              line = the same binary on plain remote DRAM, no device.
(b) right -- Redis/YCSB-C read latency, mean and p99.  Application-measured
              per-request latency (INFO commandstats usec_per_call and
              latencystats percentiles), run phase only.  Dashed line = the
              same workload on plain remote DRAM, no device (2.48 us).

OCEAN LEADS because it carries the headline runtime result (3.06-3.51x); Redis
then refines it on the read side, where per-request latency is what a
deployment actually feels.

WHY THESE TWO.  They sweep the SAME normalized WSS (0.35 / 1.1 / 2.2), so the
x axes read identically and the pair is one experiment rather than two.  Ocean
gives end-to-end runtime plus the init/solve split on the write side; Redis
gives felt per-request latency on the read side.  GAPBS bc (8 threads,
kron-26..28, 1.33-1.40x) corroborates both and is cited in the text rather than
plotted -- it sweeps a different axis (graph scale, WSS 0.20-0.81) and would
break the shared-x reading for no new claim.

THE PANELS RUN DIFFERENT THREAD COUNTS, DELIBERATELY.  (a) is -p8, full
parallelism for a benchmark built for it; (b) is 1-thread YCSB, the
conventional single-client configuration and the one Cylon's own evaluation
uses.

BOTH PANELS CARRY A NATIVE LINE, measured the same way and matched to the
CXDVirt runs: launched exactly as CXDVirt launches the workload (node-0 CPUs,
--membind 0, daxmalloc preloaded) with /dev/dax0.0 swapped for a /dev/shm file
pre-faulted on node 1, on CXDVirt's kernel (6.18.5).  The heap is on node 1,
everything else on node 0; only the device is missing.  n=3 each.  In (b) it is
drawn from the 0.35x point, the only place where NEITHER emulator does device
work -- CXDVirt logs 1 miss per 1,000,000 run-phase operations, Cylon 0
write-backs at 36% buffer occupancy -- so the distance from the line there is
emulation machinery with no modelled NAND to confound it: CXDVirt 2.45 us is
-1.3% of it (inside the rep spread), Cylon 7.76 us is 3.1x.  A single
horizontal line is honest because native is FLAT across the sweep (2.84 / 2.69
/ 2.85 us in the earlier membind arm) -- plain DRAM has no cache to overflow,
so the WSS axis does nothing without a device.  In (a) CXDVirt at 0.35x is
+4.8%, all of it in init (the compulsory faults); solve is -0.6%.  Cylon is
3.61x.  See redis_perf.py / ocean_wss_bars.py for provenance.

ONE LINE, ONE KERNEL.  Cylon runs on 6.4.6 inside a guest, so no native run
matches its setup; its ratios are read against the same line the figure draws.
The earlier --membind 1 lines (6.4.6, binary/stack/libc on node 1 as well) read
4.7% (a) and 14% (b) slow and are superseded.

WHY (a) IS STACKED.  Bar height is what the workload actually costs; the
segments show WHERE the cost differs.  At 0.35x, where solve has no device
traffic on either side, solve is within 3% (1.03x) while init -- 3.6M faults
through Cylon's single-consumer ring -- is 7.1x.  That split is the whole
result in one pair of bars: DER hits parallelise perfectly, misses do not.

LEGEND ORDER IS SHARED: baseline line, orange solid, blue solid, orange hatch,
blue hatch.  The panels are read together, so the same row position must mean
the same thing in each.  In (a) that puts solve (solid) above init (hatched),
i.e. the reverse of the stacking order -- deliberate.

CORRECTION (2026-08-25, supersedes an earlier note here): Redis's RUN-PHASE
miss rates, from the ctr1->ctr2 counter deltas, are 19.2K/s (1t) and 30.2K/s
(8t) at 2.2x -- BELOW Cylon's ~63K/s ring ceiling, not above it.  The earlier
figures (78.8K/129.3K/s) divided the CUMULATIVE nand_reads counter, three
quarters of which is load-phase rehash traffic, by the run time.  So panel
(b)'s latency gap is NOT ring saturation; it is per-miss service time (~15.8
vs ~7 us) plus in-VM execution overhead on the resident majority.  Cylon's 1t
THROUGHPUT (6.3-6.7K ops/s, nearly flat across WSS) is bound by the guest
request path (virtio RTT: ~150 us end-to-end against 8-18 us server-side), so
the unplotted 3.7-5.1x throughput column conflates VM I/O-path overhead with
device emulation -- which is exactly why the panel plots server-side latency
and not throughput.  The native line must not be read against throughput
either: native measures ~20K ops/s against CXDVirt's 34K, a gap in the wrong
direction, because the YCSB/JVM harness is not comparable across boots.

BOTH SIDES RUN MATCHED EVICTION SEMANTICS (cache_policy=clock,
cxl_clock_hw_young=0 on CXDVirt, so both age reference state identically) and a
byte-identical 14,880,176,288 B working set.  See ocean_wss_bars.py for the
mechanism, the confirmations, CXDVirt's own scan-bound drain limit, and what
moved when the suite went Splash-3 -> Splash-4 (the ratio, 3.21x -> 3.06x at
2.2x; not the per-miss ceiling, which is suite-independent).
"""
import argparse, os, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import redis_perf as R
import ocean_wss_bars as O


def build(style, a):
    X = r"$\times$"

    # ---- Redis series -----------------------------------------------------
    wss  = [d[0] for d in R.DATA]
    mean = {"CXDVirt": np.array([d[1] for d in R.DATA]),
            "Cylon":   np.array([d[2] for d in R.DATA])}
    p99  = {"CXDVirt": np.array([d[3] for d in R.DATA]),
            "Cylon":   np.array([d[4] for d in R.DATA])}
    # The published ceiling, raised only when a bar would not fit under it.
    R_MAX = max(72.0, 1.15 * max(max(v.max() for v in mean.values()),
                                 max(v.max() for v in p99.values())))

    # ---- Ocean series -----------------------------------------------------
    olab, ci, cs, yi, ys, _ratio = O.totals()   # ratios quoted in the text
    otot = np.concatenate([ci + cs, yi + ys])

    with plt.style.context(style):
        # scienceplots/ieee fixes tick+label sizes in its rcParams, so --fs
        # alone would only scale the hand-placed text.  Drive both from --fs.
        plt.rcParams.update({
            "font.size": a.fs, "axes.labelsize": a.fs, "axes.titlesize": a.fs,
            "xtick.labelsize": a.fs, "ytick.labelsize": a.fs,
            "legend.fontsize": a.fs - 1,
        })
        fig = plt.figure(figsize=tuple(float(t) for t in a.figsize.split(",")))
        gs = fig.add_gridspec(1, 2, width_ratios=[1.0, 1.0], wspace=0.34)
        # OCEAN LEFT, REDIS RIGHT.  The write-intensive panel leads, because
        # it carries the headline runtime result; Redis then refines it on the
        # read side.  Only the cell assignment moves -- the two blocks below
        # keep their own axis variables, so nothing else has to be rewired.
        axo = fig.add_subplot(gs[0])   # (a) OCEAN
        axr = fig.add_subplot(gs[1])   # (b) Redis

        # ================= (b) Redis ======================================
        xr, wr = np.arange(len(R.DATA)), 0.20
        series = [(-1.5*wr, "CXDVirt", mean, None,  "CXDVirt mean"),
                  (-0.5*wr, "Cylon",   mean, None,  "Cylon mean"),
                  ( 0.5*wr, "CXDVirt", p99,  "///", "CXDVirt p99"),
                  ( 1.5*wr, "Cylon",   p99,  "///", "Cylon p99")]
        for off, sysname, src, hat, lab in series:
            axr.bar(xr + off, src[sysname], wr, color=R.COL[sysname],
                    edgecolor="black", linewidth=0.4, hatch=hat, zorder=3,
                    label=lab)
        # NATIVE BASELINE, the same treatment (a) gets: same binary, same
        # YCSB-C configuration, no device, node-1 memory on node-0 CPUs.  Drawn
        # from the 0.35x point, which is where NEITHER emulator does device
        # work, so the distance from this line is emulation machinery alone.
        # A single line is honest because native is flat across the sweep
        # (2.84 / 2.69 / 2.85 us in the membind arm) -- plain DRAM has no cache
        # to overflow.  Placement-matched to CXDVirt; see redis_perf.py.
        rnat = axr.axhline(R.NATIVE_REMOTE, color="0.35", lw=1.0, ls="--",
                           zorder=2, label="remote DRAM")
        axr.set_ylim(0, R_MAX)
        axr.set_xticks(xr); axr.set_xticklabels(wss)
        axr.set_xlabel("normalized WSS to DRAM cache")
        axr.set_ylabel(r"Redis read latency [$\mu$s]")
        # LEGEND ORDER IS FIXED AND SHARED WITH (a): baseline line first, then
        # orange solid, blue solid, orange hatch, blue hatch.  Both panels are
        # read together, so the same row position must mean the same thing in
        # each; matplotlib's automatic collection would not give that (it sorts
        # Line2D before Patch, floating the baseline to the top of (b) only).
        h, l = axr.get_legend_handles_labels()
        by_lab = dict(zip(l, h))
        order = ["CXDVirt mean", "Cylon mean", "CXDVirt p99", "Cylon p99"]
        axr.legend([rnat] + [by_lab[k] for k in order],
                   ["remote DRAM"] + order,
                   loc="upper left", frameon=False, fontsize=a.legfs_b,
                   handlelength=1.2, borderaxespad=0.25, labelspacing=0.3,
                   ncol=1)

        # ================= (a) Ocean ======================================
        xo, wo = np.arange(len(O.DATA)), 0.30
        # Bottom segment = init (the fault path), hatched so it separates from
        # solve without needing a second hue per system.
        bc1 = axo.bar(xo - wo/2, ci, wo, color=O.COL["CXDVirt"],
                      edgecolor="black", linewidth=0.4, hatch="///", zorder=3,
                      label="CXDVirt init")
        bc2 = axo.bar(xo - wo/2, cs, wo, bottom=ci, color=O.COL["CXDVirt"],
                      edgecolor="black", linewidth=0.4, zorder=3,
                      label="CXDVirt solve")
        by1 = axo.bar(xo + wo/2, yi, wo, color=O.COL["Cylon"],
                      edgecolor="black", linewidth=0.4, hatch="///", zorder=3,
                      label="Cylon init")
        by2 = axo.bar(xo + wo/2, ys, wo, bottom=yi, color=O.COL["Cylon"],
                      edgecolor="black", linewidth=0.4, zorder=3,
                      label="Cylon solve")

        # NATIVE BASELINE.  Same binary, no device, and the same placement
        # the CXDVirt runs use (heap on node 1 via daxmalloc, everything else
        # on node 0, node-0 CPUs) -- a local-DRAM baseline would charge the
        # emulators for distance they did not cause.  Everything above this
        # line is device model plus emulation.
        # Legend entry rather than an inline label: with the inset gone the
        # bars fill the panel and there is no clear strip left at the line's
        # height -- the 2.2x pair reaches well above it on both sides.
        nat = axo.axhline(O.NATIVE_P8, color="0.35", lw=1.0, ls="--", zorder=2,
                          label="remote DRAM")
        top = otot.max()
        axo.set_ylim(0, top * a.headroom)
        axo.set_xticks(xo); axo.set_xticklabels(olab)
        axo.set_xlabel("normalized WSS to DRAM cache")
        axo.set_ylabel("OCEAN execution time [s]")   # -p8
        # One column, matching panel (b).  Five rows are tall, so --headroom
        # must keep the 1.1x pair (the tallest thing under the legend box)
        # below its bottom edge; the 2.2x pair sits to the right of it.
        # Same order as (b): baseline, orange solid, blue solid, orange hatch,
        # blue hatch -- i.e. solve (solid) before init (hatched), NOT the
        # stacking order.  Reading position must mean the same in both panels.
        axo.legend(handles=[nat, bc2, by2, bc1, by1], loc="upper left",
                   frameon=False, fontsize=a.legfs_a, handlelength=1.2,
                   borderaxespad=0.25, labelspacing=0.3, ncol=1)

        # panel labels BELOW each panel, on ONE shared baseline.  Under the
        # scienceplots LaTeX backend fontweight= is ignored, so bold has to be
        # asked for in the mark-up itself.
        fig.canvas.draw()
        inv = fig.transFigure.inverted()
        ybase = min(_ax.xaxis.label.get_window_extent().transformed(inv).y0
                    for _ax in (axr, axo)) - a.taggap
        bold = (lambda t: r"\textbf{%s}" % t) if plt.rcParams["text.usetex"] else (lambda t: t)
        for _ax, _lab in ((axo, "(a)"), (axr, "(b)")):
            bb = _ax.get_position()
            fig.text((bb.x0 + bb.x1) / 2, ybase, bold(_lab), ha="center",
                     va="top", fontsize=a.fs + 3, fontweight="bold")

        fig.savefig(a.out, bbox_inches="tight", dpi=400)
        fig.savefig(a.out.rsplit(".", 1)[0] + ".pdf", bbox_inches="tight")
        plt.close(fig)
    print("wrote", a.out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/mnt/nvme/cxdvirt/cxdvirt/reproduction/figures/macro_rw_cxdvirt_vs_cylon.png")
    ap.add_argument("--figsize", default="7.1,2.4")
    ap.add_argument("--fs", type=float, default=10)
    ap.add_argument("--legfs_a", type=float, default=11,
                    help="panel (a) = OCEAN, left; legend font size")
    ap.add_argument("--legfs_b", type=float, default=11,
                    help="panel (b) = Redis, right; legend font size")
    ap.add_argument("--headroom", type=float, default=1.32,
                    help="(a) OCEAN y-limit as a multiple of the tallest bar; makes "
                         "room for the legend above the 2.2x pair")
    ap.add_argument("--taggap", type=float, default=0.02,
                    help="figure-fraction drop from the x-label to the (a)/(b) "
                         "tag baseline; smaller closes the bottom white band")
    a = ap.parse_args()
    import scienceplots  # noqa: F401
    try:
        build(["science", "ieee"], a)
    except Exception as e:
        print(f"[latex failed ({e})]", file=sys.stderr)
        build(["science", "ieee", "no-latex"], a)


if __name__ == "__main__":
    main()
