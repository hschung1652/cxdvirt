#!/usr/bin/env python3
"""plot_prefetch_regimes.py -- next-N prefetch: observation, control, mechanism.

Three panels that make one argument, in order:

  (c) OBSERVATION.  On a device whose background reclaim keeps up with demand,
      next-N degrades Redis latency monotonically in N.  Inset: with reclaim
      forced onto the fault path, the SAME prefetcher appears to help.
  (d) CONTROL.  The apparent benefit is not better prefetching.  Accuracy and
      timeliness are identical in both regimes at every depth, so prediction
      quality cannot explain the difference.
  (e) MECHANISM.  It is eviction relief.  Prefetched pages are read-only, hence
      clean and unreferenced, so they displace dirty victims that would each
      cost ~100us of in-fault stall.  Where reclaim keeps up the fault path
      performs zero evictions, so that substrate does not exist -- the flat
      zero line is the panel's whole point.

The two regimes differ ONLY in the drain's throughput ceiling
(bg_drain_batch/bg_drain_ms): 64 per 10ms = ~5.8K evictions/s against ~12.7K/s
of demand, versus 1024 per 1ms which never binds.  Same binary, workload, cache
and device model.

NOT a trade-off figure.  The under-provisioned regime is strictly dominated --
its best prefetch-assisted point (72.3us) is 74% slower than simply letting
reclaim keep up with no prefetch at all (41.4us) -- so nothing here suggests
choosing it.  It is a demonstration that an evaluation run on that baseline
misattributes eviction relief to prediction.
"""
import argparse, glob, os, re, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import latload  # noqa: E402  (prefers <base>.lat.gz, falls back to <base>.raw)

# Degree ramp: Tol bright, cool->warm so an ordered variable reads as a ramp.
COL = {0: "#4477AA", 1: "#66CCEE", 2: "#228833", 4: "#CCBB44", 8: "#EE6677"}
LS  = {0: "-", 1: (0, (1, 1)), 2: "--", 4: "-.", 8: (0, (3, 1, 1, 1))}
# Fate keeps the teal/orange pair used since the original fate figure.
TIMELY_C, LATE_C = "#009988", "#EE7733"
# Regime encoding, used in (d) as hatch and in (e) as colour.
SLOW_C, FAST_C = "#CC3311", "#4477AA"
SLOW_HATCH = "///"
# The two arms differ ONLY in the drain ceiling (bg_drain_batch/bg_drain_ms).
# Label with the throughput that implies, so the legend names the variable
# rather than its effect: 64/10ms -> ~5.8K evictions/s against ~12.7K/s of
# demand; 1024/1ms -> ~1.0M/s, which never binds.
RLAB = {"slow": "slow eviction", "fast": "fast eviction"}


def _rlab(rg, R):
    """Legend label for an arm, carrying its measured eviction rate."""
    er = evict_rate(R[(rg, 0)]) if (rg, 0) in R else None
    return RLAB[rg] if not er else rf"{RLAB[rg]} ({er[0]/1e3:.0f}K/s)"


def _rd(p):
    return {k: int(v) for k, v in (l.split() for l in open(p))}


def collect(d, threads=1, policy="clock"):
    """-> {(regime, degree): record}.  Regime is decided by the drain ceiling,
    which is what the two arms actually differ in; keying on srcversion instead
    would split the slow arm across two builds that behave identically."""
    out = {}
    for meta in sorted(glob.glob(os.path.join(d, "*.meta"))):
        b = meta[:-5]
        m = {}
        for line in open(meta):
            k, _, v = line.partition(" ")
            m[k.strip()] = v.strip()
        if m.get("threads") != str(threads) or m.get("cache_policy") != policy:
            continue
        if m.get("prefetch_random") == "1":
            continue                       # control arm is a separate story
        if not os.path.exists(b + ".run.out"):
            continue
        run = open(b + ".run.out").read()
        em = re.search(r"^\[READ\], Return=ERROR, (\d+)", run, re.M)
        if em and int(em.group(1)) > 0:
            print(f"  EXCLUDED (READ errors): {os.path.basename(b)}", file=sys.stderr)
            continue
        deg = int(m.get("prefetch_degree", 0) or 0)
        if m.get("prefetch_mode") != "1":
            deg = 0
        regime = "slow" if m.get("bg_drain_batch", "64") == "64" else "fast"

        c1, c2 = _rd(b + ".ctr1"), _rd(b + ".ctr2")
        x = {k: c2[k] - c1.get(k, 0) for k in c2}
        lat = latload.read_latencies(b)
        if not lat.size:
            continue
        # Wall time of the run phase, for the eviction-rate annotation.  YCSB
        # reports it in ms; a run whose line is missing simply drops out of the
        # annotation rather than being charged a guessed duration.
        _rt = re.search(r"^\[OVERALL\], RunTime\(ms\), ([0-9.]+)", run, re.M)
        rec = dict(lat=lat, ops=lat.size, mean=lat.mean(), x=x,
                   sec=float(_rt.group(1)) / 1000 if _rt else None,
                   has_wb="sync_wb" in x)
        # Prefer a replicate that carries sync_wb: (e) needs it, and on the
        # slow arm only the later build has the counter.
        prev = out.get((regime, deg))
        if prev is None or (rec["has_wb"] and not prev["has_wb"]):
            out[(regime, deg)] = rec
    return out


def evict_rate(r):
    """-> (evictions/s, share taken on the fault path) for one record.

    The two arms are named for the drain's throughput ceiling, which is the
    knob, but the ceiling is not what a reader can check against the panel.
    What is checkable is the rate the run actually sustained and how much of it
    the drain failed to absorb, so the annotation quotes those.
    """
    x, sec = r["x"], r.get("sec") or 0.0
    ev = x.get("async_evicts", 0) + x.get("sync_evicts", 0)
    if not sec or not ev:
        return None
    return ev / sec, x.get("sync_evicts", 0) / ev


def _fate(r):
    """(accuracy, timely, late) as % of prefetches issued."""
    x = r["x"]
    u, w, l = x.get("pf_useful", 0), x.get("pf_wasted", 0), x.get("pf_late", 0)
    if not (u + w):
        return None
    return 100 * u / (u + w), 100 * (u - l) / (u + w), 100 * l / (u + w)


def build(style, a, R, ext_axes=None, letters=True):
    with plt.style.context(style):
        if ext_axes is None:
            plt.rcParams.update({
                "font.size": a.fs, "axes.labelsize": a.fs, "axes.titlesize": a.fs,
                "xtick.labelsize": a.fs, "ytick.labelsize": a.fs,
            })
            w, h = (float(t) for t in a.figsize.split(","))
            fig, axes = plt.subplots(1, 3, figsize=(w, h), gridspec_kw={
                "width_ratios": [float(t) for t in a.width_ratios.split(",")]})
        else:
            axes = ext_axes
            fig = axes[0].figure
        axC, axD, axE = axes
        # scienceplots renders through LaTeX, where a "\n" in a label is not a
        # line break -- an explicit stacking environment is needed instead.
        tex = plt.rcParams.get("text.usetex", False)

        def _2l(top, bot):
            """A legend label forced onto two lines, centred on its handle.

            \shortstack centres the lines against each other but takes its
            baseline from the BOTTOM one, and matplotlib aligns handle and label
            on their baselines -- so a bare stack puts the handle level with the
            SECOND line.  Lowering the stack by a full \height (one line, for a
            two-line stack) overshoots to the FIRST line; half that lands the
            handle between them, which is what --legd-raise dials."""
            if tex:
                rz = getattr(a, "legd_raise", None)
                rz = -0.25 if rz is None else rz
                return (r"\raisebox{" + f"{rz:g}" + r"\height}{\shortstack{"
                        + top + r" \\ " + bot + r"}}")
            return top + "\n" + bot
        degs = sorted({d for (rg, d) in R if rg == "fast"})
        pf = [d for d in degs if d]

        # Per-panel legend sizes.  They default to an offset from --fs, but the
        # combined figure gives its panels different widths than the standalone
        # one does, so each is overridable.  getattr with a default, not a.x,
        # because `a` may be an argparse Namespace that never saw these flags.
        def _lf(key, dflt):
            return getattr(a, key, None) or (a.fs + dflt)

        # ---- (c) OBSERVATION -------------------------------------------
        # The published range holds every reference curve from P0.1 to P99.9
        # with margin; a run outside it extends the axis rather than clipping.
        fast = [R[("fast", d)]["lat"] for d in degs if ("fast", d) in R]
        linmin = min(a.linmin, min(float(np.percentile(v, 0.1)) for v in fast))
        linmax = max(a.linmax, 1.5 * max(float(np.percentile(v, 99.9)) for v in fast))
        for d in degs:
            r = R.get(("fast", d))
            if not r:
                continue
            gx = np.geomspace(linmin, linmax, 600)
            axC.plot(gx, np.searchsorted(r["lat"], gx, side="right") / r["ops"],
                     color=COL.get(d), ls=LS.get(d, "-"), lw=1.1, zorder=3,
                     label=f"$N$={d}", solid_capstyle="round")
        axC.set_xscale("log")
        # Open the axis slightly BELOW the first tick.  With the limit exactly
        # at 20 that tick label is centred on the spine and collides with the
        # y-axis "0.0" once the type is large; the ticks themselves are
        # unchanged, only the limit moves.
        axC.set_xlim(linmin / (getattr(a, "xlo_pad", None) or 1.0), linmax)
        axC.set_ylim(0, 1.005)
        ticks = [t for t in (5, 10, 20, 30, 50, 100, 200, 300, 500, 1000, 2000, 3000, 5000)
                 if linmin <= t <= linmax]
        axC.set_xticks(ticks)
        axC.set_xticklabels([f"{t // 1000}k" if t >= 1000 else str(t) for t in ticks])
        axC.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        # Named in full, matching the policy CDF it sits beside in the combined
        # figure: both axes are the same quantity from the same instrument.
        axC.set_xlabel(r"Redis read latency [$\mu$s]"); axC.set_ylabel("CDF")
        axC.grid(True, alpha=0.25, lw=0.4, zorder=0)
        # No mode="expand": it can only stretch a legend that is narrower than
        # the axes, and five entries are not.  Asked to fit a wider legend into
        # the bbox it collapses the column offsets and each handle lands on the
        # previous label.  Centred at natural width instead.
        # Sized to fit WITHIN the panel.  It is centred on the axes, so any
        # excess width hangs off both ends -- and since (b) is stretched
        # rightwards afterwards, a legend wider than the panel ends up past the
        # figure edge, not merely past the spine.
        axC.legend(loc="lower center", bbox_to_anchor=(0.5, 1.005),
                   frameon=False, handlelength=1.1,
                   borderaxespad=0.0, fontsize=_lf("legfs_c", -3.6), ncol=5,
                   columnspacing=0.5, handletextpad=0.3)

        # Inset: the under-provisioned regime, N=0 vs N=8 only.  Five curves at
        # inset size are unreadable, and the two endpoints carry the reversal.
        if ("slow", 0) in R and ("slow", degs[-1]) in R:
            axI = axC.inset_axes([float(t) for t in a.inset_box.split(",")])
            lo = min(np.percentile(R[("slow", d)]["lat"], a.inset_qlo)
                     for d in (0, degs[-1]))
            hi = max(np.percentile(R[("slow", d)]["lat"], 99.7)
                     for d in (0, degs[-1])) * 1.1
            gi = np.geomspace(lo, hi, 400)
            for d in (0, degs[-1]):
                r = R[("slow", d)]
                axI.plot(gi, np.searchsorted(r["lat"], gi, side="right") / r["ops"],
                         color=COL.get(d), ls=LS.get(d, "-"), lw=1.05, zorder=3)
            axI.set_xscale("log"); axI.set_xlim(lo, hi)
            ylo = min(np.searchsorted(R[("slow", d)]["lat"], lo, side="right")
                      / R[("slow", d)]["ops"] for d in (0, degs[-1]))
            axI.set_ylim(max(0.0, ylo - 0.04), 1.02)
            axI.set_xticks([100, 300, 1000]); axI.set_xticklabels(["100", "300", "1k"])
            axI.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
            axI.set_yticks([0, 0.5, 1]); axI.set_yticklabels(["0", ".5", "1"])
            axI.tick_params(labelsize=a.fs - 2.2, length=2.0, pad=1.4)
            axI.grid(True, alpha=0.2, lw=0.3, zorder=0)
            for sp in axI.spines.values():
                sp.set_linewidth(0.5)
            # Two lines: the arm and its rate on one, the comparison on the
            # next.  On one line the title overruns the inset's right edge,
            # which is narrower than the panel it sits in.
            _sl = evict_rate(R[("slow", 0)]) if ("slow", 0) in R else None
            axI.set_title(("slow eviction" if not _sl
                           else rf"slow eviction ({_sl[0]/1e3:.0f}K/s)")
                          + "\n" + r"$N$=0 vs 8",
                          fontsize=_lf("ins_title_fs", -3.5), pad=2.0,
                          linespacing=1.1)

        # ---- (d) CONTROL ------------------------------------------------
        # Paired bars: the claim is that the two regimes are the SAME, so both
        # must be on the panel.  Colour = fate, hatch = regime.
        xs = np.arange(len(pf)); w = 0.36
        for k, (rg, hatch, off) in enumerate((("slow", SLOW_HATCH, -w / 2),
                                              ("fast", None, w / 2))):
            tim = [(_fate(R[(rg, d)]) or (0, 0, 0))[1] for d in pf]
            late = [(_fate(R[(rg, d)]) or (0, 0, 0))[2] for d in pf]
            axD.bar(xs + off, tim, w, color=TIMELY_C, hatch=hatch,
                    edgecolor="black", lw=0.4, zorder=3,
                    label="in time" if k else None)
            axD.bar(xs + off, late, w, bottom=tim, color=LATE_C, hatch=hatch,
                    edgecolor="black", lw=0.4, zorder=3,
                    label="late" if k else None)
        axD.set_xticks(xs); axD.set_xticklabels([str(d) for d in pf])
        # Named in full, matching (e) beside it -- the bare "$N$" relied on the
        # reader carrying the meaning over from the other panel's label.
        axD.set_xlabel(r"prefetch degree $N$")
        axD.set_ylabel(r"prefetches used [\%]")
        # Fix the tick COUNT rather than letting AutoLocator choose it: its
        # "auto" bin count scales with axis-length-over-font-size, so raising
        # --fs silently thins 0/10/20/30 down to 0/20 and the panel loses its
        # readable gridlines exactly when the type gets bigger.
        axD.yaxis.set_major_locator(
            matplotlib.ticker.MaxNLocator(nbins=4, steps=[1, 2, 5, 10]))
        axD.grid(True, axis="y", alpha=0.25, lw=0.4, zorder=0)

        h, l = axD.get_legend_handles_labels()
        h += [matplotlib.patches.Patch(facecolor="white", edgecolor="black",
                                       lw=0.4, hatch=SLOW_HATCH),
              matplotlib.patches.Patch(facecolor="white", edgecolor="black", lw=0.4)]
        # Legend goes ABOVE the panel, as in (c).  In-axes it needed ~2x
        # ylim headroom, which squashed the bars to half height; outside there
        # is horizontal room, so the regime labels also fit on one line.
        # Name the arms with the eviction rate each actually sustained, rather
        # than with "slow"/"fast" alone, which says nothing about the size of
        # the difference.  Measured at N=0, the no-prefetch point both arms
        # share, so the figure quotes a property of the arms and not of any
        # prefetch effect.  The legend sits outside the panel, where there is
        # horizontal room; an in-axes note is not an option here because the
        # ylim below is pinned to bar height to keep the bars from being
        # squashed.
        l += [_rlab("slow", R), _rlab("fast", R)]
        axD.set_ylim(0, max(sum(x) for x in
                            [(_fate(R[(rg, d)]) or (0, 0, 0))[1:]
                             for rg in ("slow", "fast") for d in pf]) * 1.12)
        # No mode="expand": forcing two columns to fill the bbox collapses
        # their x offsets and the labels print on top of each other.
        axD.legend(h, l, loc="lower center", bbox_to_anchor=(0.5, 1.005),
                   frameon=False, handlelength=1.1, borderaxespad=0.0,
                   fontsize=_lf("legfs_d", -3.0), ncol=2, columnspacing=0.8,
                   handletextpad=0.4, labelspacing=0.25)

        # ---- (e) MECHANISM ----------------------------------------------
        # Slow-eviction arm only: this panel explains why THAT arm improves.
        # Bars are an OVERLAY, not a stack -- grey is the whole runtime and
        # blue the part of it spent stalled -- so "total" labels what it is.
        # Normalised to N=0 of this arm, so bar height still carries the gain.
        sd = [d for d in degs if ("slow", d) in R]
        xs2 = np.arange(len(sd))
        base = R[("slow", 0)]
        base_rt = base["ops"] * base["mean"] / 1e6
        tot, stall, clean = [], [], []
        for d in sd:
            r = R[("slow", d)]
            tot.append(100 * (r["ops"] * r["mean"] / 1e6) / base_rt)
            stall.append(100 * (r["x"].get("sync_wb_ns", 0) / 1e9) / base_rt)
            clean.append((r["x"]["sync_evicts"] - r["x"].get("sync_wb", 0)) / 1e3)
        axE.bar(xs2, tot, 0.62, color="#DDDDDD", edgecolor="#999999", lw=0.4,
                zorder=3, label=_2l("total", "time"))
        axE.bar(xs2, stall, 0.62, color=FAST_C, zorder=4,
                label=_2l("dirty write-", "back stall"))
        axE.set_xticks(xs2); axE.set_xticklabels([str(d) for d in sd])
        axE.set_xlabel("prefetch degree $N$")
        # (d) plots the slow arm alone, which the panel never said.  Name it,
        # with its rate, so the row's three panels each identify their arm.
        axE.set_ylabel(r"runtime [\% of $N{=}0$]" "\n"
                       rf"\footnotesize {_rlab('slow', R)}")
        # Headroom only for the value the legend used to need; with the legend
        # moved above the panel the bars can use the height instead.
        axE.set_ylim(0, max(tot) * 1.15)
        axE.grid(True, axis="y", alpha=0.25, lw=0.4, zorder=0)

        axF = axE.twinx()
        axF.plot(xs2, clean, marker="D", ms=3.0, lw=1.2, color=SLOW_C, zorder=6,
                 label=_2l("in-fault", "clean evictions"))
        axF.set_ylabel(r"clean evictions [$\times10^3$]", color=SLOW_C)
        axF.tick_params(axis="y", colors=SLOW_C)
        # Kept near the bars' scale factor so the crossing point -- stall
        # falling past clean-eviction count rising -- stays where it is on the
        # panel rather than being an artifact of two unrelated head-rooms.
        axF.set_ylim(0, max(clean) * 1.30)
        hE, lE = axE.get_legend_handles_labels()
        hF, lF = axF.get_legend_handles_labels()
        # Above the panel in one row, as in (c): three entries whose longest is
        # "in-fault clean evictions" cover the bars at any in-axes corner.
        axE.legend(hE + hF, lE + lF, loc="lower center",
                   bbox_to_anchor=(0.5, 1.005), frameon=False,
                   handlelength=1.1, borderaxespad=0.0, ncol=3,
                   columnspacing=1.0, handletextpad=0.45,
                   fontsize=_lf("legfs_e", -3.0))

        if letters:
            for i, ax in enumerate(axes):
                ax.text(0.5, -0.26, rf"\textbf{{({chr(99 + i)})}}",
                        transform=ax.transAxes, ha="center", va="top",
                        fontsize=a.fs + 1)
        if ext_axes is None:
            fig.tight_layout(pad=0.35, w_pad=a.wpad)
            fig.savefig(a.out, bbox_inches="tight", dpi=400)
            fig.savefig(a.out.rsplit(".", 1)[0] + ".pdf", bbox_inches="tight")
            plt.close(fig)
            print("wrote", a.out)


def summarise(R):
    degs = sorted({d for (rg, d) in R if rg == "fast"})
    print(f"\n{'':>6}{'deg':>5}{'mean us':>9}{'vs N=0':>9}{'acc%':>7}"
          f"{'timely%':>9}{'in-fault wb/op':>16}")
    for rg in ("slow", "fast"):
        b = R.get((rg, 0))
        for d in degs:
            r = R.get((rg, d))
            if not r:
                continue
            f = _fate(r)
            wb = r["x"].get("sync_wb")
            print(f"{rg:>6}{d:>5}{r['mean']:>9.1f}"
                  f"{100*(r['mean']/b['mean']-1):>+8.1f}%"
                  f"{(f[0] if f else float('nan')):>7.1f}"
                  f"{(f[1] if f else float('nan')):>9.1f}"
                  f"{(wb/r['ops'] if wb is not None else float('nan')):>16.3f}")
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="/mnt/nvme/cxdvirt/cxdvirt/reproduction/results/redis/fig8_4914/prefetch")
    ap.add_argument("--out", default="/mnt/nvme/cxdvirt/cxdvirt/reproduction/figures/"
                                     "prefetch_regimes.png")
    ap.add_argument("--figsize", default="7.16,2.6")
    ap.add_argument("--width-ratios", dest="width_ratios", default="1.5,0.95,1.05")
    ap.add_argument("--fs", type=float, default=10)
    ap.add_argument("--wpad", type=float, default=1.2)
    ap.add_argument("--linmin", type=float, default=20.0)
    ap.add_argument("--linmax", type=float, default=600.0)
    ap.add_argument("--inset-qlo", dest="inset_qlo", type=float, default=4.0)
    ap.add_argument("--inset-box", dest="inset_box", default="0.415,0.145,0.535,0.475")
    a = ap.parse_args()

    R = collect(a.dir)
    if not R:
        sys.exit("no runs found")
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    import scienceplots  # noqa: F401
    try:
        build(["science", "ieee"], a, R)
    except Exception as e:
        print(f"[LaTeX render failed ({e}); no-latex fallback]", file=sys.stderr)
        build(["science", "ieee", "no-latex"], a, R)
    summarise(R)


if __name__ == "__main__":
    main()
