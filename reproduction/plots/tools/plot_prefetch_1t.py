#!/usr/bin/env python3
"""plot_prefetch_1t.py -- the single-thread mechanism figure (IEEE double column).

WHY SINGLE THREAD.  At one client, runtime IS the serial sum of per-op costs,
so the runtime budget can be checked against wall clock:

    -25.3 s (dirty write-back stall removed) + 15.1 s (clean evictions added)
     = -10.2 s  <- exactly the observed runtime delta

That identity does not exist at 8 threads, where work overlaps.  The 8-thread
runs remain the deployment result; this figure is the explanation.

PANELS
  (a) 1-thread read-latency CDF.  Prefetch makes the MEDIAN worse and the TAIL
      much better; the crossover sits between p50 and p75.  At 8 threads
      queueing pushes p10 to 135 us and buries the whole effect.
  (b) p50/p99 vs degree, normalised to that configuration's degree 0, for every
      thread count present.  Normalising is what lets 1t and 8t share one axis
      (their absolute scales differ 4x) and is the direct evidence that the same
      mechanism operates at both -- 8 threads merely amplify it.
  (c) Measured runtime decomposition.  Stacked bars are 100% counter-derived,
      no fitted coefficient: sync_wb_applied_ns is the real serial stall, the
      remainder is everything else.  The overlaid line is in-fault CLEAN
      evictions, which is what the growing remainder tracks.

Usage:
  python3 plot_prefetch_1t.py --dir <sweep dir> [--out <path.png>]
"""
import argparse, glob, os, re, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import latload  # noqa: E402  (prefers <base>.lat.gz, falls back to <base>.raw)

# Paul Tol BRIGHT, the palette the rest of the repo uses
# (TOL_BRIGHT in plots/tools/spread_two_panel.py:38), walked cool->warm so the
# ordered variable (degree) reads as a ramp rather than as arbitrary hues.
# Linestyles duplicate the colour information for greyscale printing -- which
# also covers bright's yellow, its one weak member on white at 1pt.
COL = {0: "#4477AA", 1: "#66CCEE", 2: "#228833", 4: "#CCBB44", 8: "#EE6677"}
LS  = {0: "-", 1: (0, (1, 1)), 2: "--", 4: "-.", 8: (0, (3, 1, 1, 1))}
# CLEAN_C stays the stronger red: bright's #EE6677 reads pink as a thin
# line over a pale fill, and this series has to carry the panel on its own.
STALL_C, REST_C, CLEAN_C = "#4477AA", "#DDDDDD", "#CC3311"
# Fate encoding keeps the teal/orange pair from the original
# prefetch_fate.png -- "in time" vs "late" is a separate semantic axis
# from the Tol-bright degree ramp, and these two read apart better.
TIMELY_C, LATE_C = "#009988", "#EE7733"


def read_meta(p):
    m = {}
    for line in open(p):
        k, _, v = line.partition(" ")
        m[k.strip()] = v.strip()
    return m


def read_ctr(p):
    return {k: int(v) for k, v in (l.split() for l in open(p))}


def collect(d):
    """One record per (threads, degree), keeping the NEWEST module build seen
    for that thread count.  Mixing builds silently pools different binaries."""
    cand = {}
    for meta in sorted(glob.glob(os.path.join(d, "*.meta"))):
        b = meta[:-5]
        m = read_meta(meta)
        try:
            run = open(b + ".run.out").read()
        except OSError:
            continue
        # Reject contaminated runs: a load that did not fully populate the
        # keyspace is a SMALLER effective WSS masquerading as a good result.
        em = re.search(r"^\[READ\], Return=ERROR, (\d+)", run, re.M)
        if em and int(em.group(1)) > 0:
            print(f"  EXCLUDED (READ errors): {os.path.basename(b)}", file=sys.stderr)
            continue
        try:
            ld = open(b + ".load.out").read()
        except OSError:
            continue
        im = re.search(r"^\[INSERT\], Return=OK, (\d+)", ld, re.M)
        if not im or int(im.group(1)) < int(m.get("records", 1000000) or 1000000):
            print(f"  EXCLUDED (short load): {os.path.basename(b)}", file=sys.stderr)
            continue
        rt = re.search(r"^\[OVERALL\], RunTime\(ms\), (\d+)", run, re.M)
        if not rt:
            continue

        thr = int(m.get("threads", 8) or 8)
        deg = int(m.get("prefetch_degree", 0) or 0)
        if m.get("prefetch_mode") != "1":
            deg = 0
        if m.get("prefetch_random") == "1" and deg:
            continue                      # control arm belongs to the fate figure
        src = m.get("srcversion", "unknown")

        lat = latload.read_latencies(b)
        if not lat.size:
            continue
        c1, c2 = read_ctr(b + ".ctr1"), read_ctr(b + ".ctr2")
        x = {k: c2.get(k, 0) - c1.get(k, 0) for k in c2}
        cand.setdefault(thr, {}).setdefault(src, {})[deg] = dict(
            deg=deg, thr=thr, src=src, lat=lat, ops=lat.size,
            rt=int(rt.group(1)) / 1000.0,
            stall=x.get("sync_wb_ns", 0) / 1e9,
            swb=x.get("sync_wb"), sev=x.get("sync_evicts", 0),
            useful=x.get("pf_useful", 0), wasted=x.get("pf_wasted", 0),
            pflate=x.get("pf_late", 0),
            mtime=os.path.getmtime(meta))

    out = {}
    for thr, builds in cand.items():
        src = max(builds, key=lambda s: max(r["mtime"] for r in builds[s].values()))
        if len(builds) > 1:
            dropped = sorted(set(builds) - {src})
            print(f"  {thr}t: using build {src[:8]}, ignoring {len(dropped)} older "
                  f"({', '.join(s[:8] for s in dropped)})", file=sys.stderr)
        out[thr] = builds[src]
    return out


def build(style, a, R, ext_axes=None, letters=True):
    """Draw the prefetch panels.

    ext_axes -- when given, draw into these caller-supplied axes instead of
    creating a figure.  The caller then owns layout, panel letters and saving,
    which is how combined_policy_prefetch.py reuses this code rather than
    duplicating it.  Standalone use (ext_axes=None) is unchanged.
    """
    with plt.style.context(style):
        plt.rcParams.update({
            "font.size": a.fs, "axes.labelsize": a.fs, "axes.titlesize": a.fs,
            "xtick.labelsize": a.fs, "ytick.labelsize": a.fs,
            "legend.fontsize": a.fs - 1.0,
        })
        w, h = (float(t) for t in a.figsize.split(","))
        # (b) became redundant once the 8-thread CDF moved into (a) as an
        # inset: saturation reads as the N=4/N=8 curves overlapping, the median
        # penalty IS the crossover, and cross-thread agreement is the two CDFs
        # having the same shape.  What (b) still had was precision (-19.7% vs
        # -19.5% at p99), which belongs in prose.  Keep it selectable.
        sel = [c for c in a.panels]
        # Ratios chosen so (a) keeps the same absolute width it had as half of
        # a two-panel figure (1.32/2.32 of the total); the fate and budget
        # panels split what is left.
        DEFAULT_WR = {"ac": [1.32, 1.0], "afc": [1.32, 0.36, 0.64],
                      "abc": [1.5, 1.0, 1.05]}
        wr = [float(t) for t in a.width_ratios.split(",")] if a.width_ratios \
             else DEFAULT_WR.get(a.panels, [1.0] * len(sel))
        if ext_axes is None:
            fig, axes = plt.subplots(1, len(sel), figsize=(w, h),
                                     gridspec_kw={"width_ratios": wr[:len(sel)]})
            axes = np.atleast_1d(axes)
        else:
            axes = np.atleast_1d(ext_axes)
            fig = axes[0].figure
        AX = dict(zip(sel, axes))
        axA, axB, axC, axF = (AX.get("a"), AX.get("b"),
                              AX.get("c"), AX.get("f"))

        M = R[a.mech]          # the thread count the mechanism panels describe
        degs = sorted(M)

        # ---- (a) latency CDF at the mechanism thread count -----------------
        for d in degs:
            lat = M[d]["lat"]
            xs = np.geomspace(a.linmin, a.linmax, 600)
            axA.plot(xs, np.searchsorted(lat, xs, side="right") / lat.size,
                     color=COL.get(d), ls=LS.get(d, "-"), lw=1.1, zorder=3,
                     label=f"$N$={d}", solid_capstyle="round")
        axA.set_xscale("log")
        axA.set_xlim(a.linmin, a.linmax); axA.set_ylim(0, 1.005)
        # Mark where the baseline and deepest-degree CDFs actually cross: left
        # of it prefetch is behind (median penalty), right of it ahead (tail
        # win).  Taking d0's median instead would mark the wrong x -- the two
        # coincide only if the curves happen to cross at the median.
        gx = np.geomspace(a.linmin, a.linmax, 2000)
        f0 = np.searchsorted(M[degs[0]]["lat"], gx, side="right") / M[degs[0]]["ops"]
        fN = np.searchsorted(M[degs[-1]]["lat"], gx, side="right") / M[degs[-1]]["ops"]
        # The crossing is UPWARD: left of it the prefetched run has completed
        # FEWER ops by a given latency (fN < f0, the median penalty); right of
        # it, more (the tail win).  Searching for a downward flip finds nothing
        # and silently falls back to the median, which is ~2x too far left.
        sign = np.sign(fN - f0)
        flip = np.flatnonzero((sign[:-1] < 0) & (sign[1:] > 0))
        cx = gx[flip[-1]] if flip.size else float("nan")
        if not np.isfinite(cx):
            print("  note: CDFs never cross; omitting the crossover marker",
                  file=sys.stderr)
        if np.isfinite(cx):
            axA.axvline(cx, color="0.5", lw=0.6, ls=(0, (2, 2)), zorder=2)
        if np.isfinite(cx):
            axA.annotate("costs", (cx, 0.045), xytext=(-2.5, 0),
                         textcoords="offset points",
                     ha="right", va="center", fontsize=a.fs - 1.0, color="0.35")
            axA.annotate("pays", (cx, 0.045), xytext=(2.5, 0),
                         textcoords="offset points", ha="left", va="center",
                         fontsize=a.fs - 1.0, color="0.35")
            print(f"  CDF crossover at {cx:.0f} us "
                  f"(CDF={np.interp(cx, gx, f0):.2f})", file=sys.stderr)
        # Default log locator labels only the decade (a single "10^2" here).
        axA.set_xticks([20, 30, 50, 77, 100, 200, 300, 500])
        axA.set_xticklabels(["20", "30", "50", "77", "100", "200", "300", "500"])
        axA.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        axA.set_xlabel(r"read latency [$\mu$s]"); axA.set_ylabel("CDF")
        axA.grid(True, alpha=0.25, lw=0.4, zorder=0)
        # The 8-thread CDF as an inset: panel (a)'s lower right is empty
        # (every curve is above CDF 0.4 by 50us) and the two distributions are
        # far more informative side by side than apart -- the SAME crossover
        # appears at both concurrencies, just at a different percentile.
        # Sharing (a)'s colour/linestyle mapping means it needs no legend.
        for other in sorted(t for t in R if t != a.mech):
            T = R[other]
            axI = axA.inset_axes(
                [float(t) for t in a.inset_box.split(",")])
            # Start at a percentile, not at the minimum: the 8-thread curves
            # are flat near zero for the first ~150us, so anchoring at min()
            # spends a third of the inset on empty space instead of on the
            # crossover and the tail, which is the whole reason it is here.
            lo = min(np.percentile(v["lat"], a.inset_qlo) for v in T.values())
            hi = max(np.percentile(v["lat"], 99.7) for v in T.values()) * 1.1
            gi = np.geomspace(lo, hi, 400)
            for d in sorted(T):
                lat = T[d]["lat"]
                axI.plot(gi, np.searchsorted(lat, gi, side="right") / lat.size,
                         color=COL.get(d), ls=LS.get(d, "-"), lw=1.05,
                         zorder=3, solid_capstyle="round")
            # Same upward-crossing test as the main panel.
            ds = sorted(T)
            f0 = np.searchsorted(T[ds[0]]["lat"], gi, side="right") / T[ds[0]]["ops"]
            fN = np.searchsorted(T[ds[-1]]["lat"], gi, side="right") / T[ds[-1]]["ops"]
            sg = np.sign(fN - f0)
            fl = np.flatnonzero((sg[:-1] < 0) & (sg[1:] > 0))
            if fl.size:
                axI.axvline(gi[fl[-1]], color="0.5", lw=0.5, ls=(0, (2, 2)), zorder=2)
                print(f"  inset ({other}t) crossover at {gi[fl[-1]]:.0f} us "
                      f"(CDF={np.interp(gi[fl[-1]], gi, f0):.2f})", file=sys.stderr)
            axI.set_xscale("log")
            axI.set_xlim(lo, hi)
            # Clipping the x-range leaves the curves entering at CDF>0; float
            # the y floor to just under that so the box is not half empty.
            ylo = min(np.searchsorted(v["lat"], lo, side="right") / v["ops"]
                      for v in T.values())
            axI.set_ylim(max(0.0, ylo - 0.04), 1.02)
            yt = [t for t in (0, 0.25, 0.5, 0.75, 1) if t >= ylo - 0.04]
            axI.set_yticks(yt)
            axI.set_yticklabels([("1" if t == 1 else "0" if t == 0
                                  else f"{t:g}".lstrip("0")) for t in yt])
            axI.set_xticks([200, 500, 1000])
            axI.set_xticklabels(["200", "500", "1k"])
            axI.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
            axI.tick_params(labelsize=a.fs - 1.0, length=2.0, pad=1.4)
            axI.grid(True, alpha=0.2, lw=0.3, zorder=0)
            for sp in axI.spines.values():
                sp.set_linewidth(0.5)
            axI.set_title(f"{other} threads", fontsize=a.fs - 1.2, pad=2.0)
            break            # one inset; more would not fit

        # handlelength/fontsize overridable: five line samples at 1.9 make this
        # the widest legend in the row, which is fine standalone but crowds the
        # curves when (a) is narrowed to share a row with more panels.
        axA.legend(loc="upper left", frameon=False,
                   handlelength=getattr(a, "leghl_a", None) or 1.9,
                   labelspacing=0.22, borderaxespad=0.45,
                   fontsize=getattr(a, "legfs_a", None) or (a.fs - 2.4))

        # ---- (b) p50/p99 vs degree, normalised, every thread count ---------

        # Normalising to each configuration's own N=0 is what allows 1t and 8t
        # on one axis (their absolute scales differ ~4x) and is also the claim:
        # same mechanism at both, the thread count only scales it.
        if axB is not None:
            for thr in sorted(R):
                S = R[thr]; ds = sorted(S)
                if 0 not in S:
                    print(f"  SKIP {thr}t in panel (b): no N=0 run to normalise "
                          f"against (have {ds})", file=sys.stderr)
                    continue
                if len(ds) < len(degs):
                    print(f"  note: {thr}t is incomplete -- {ds} vs {degs}",
                          file=sys.stderr)
                for q, mk, lsty in ((50, "o", "--"), (99, "s", "-")):
                    base = np.percentile(S[0]["lat"], q)
                    ys = [100 * (np.percentile(S[d]["lat"], q) / base - 1) for d in ds]
                    axB.plot(ds, ys, marker=mk, ms=2.6, lw=1.0, ls=lsty,
                             color="#4477AA" if thr == a.mech else "#EE6677",
                             zorder=3, label=f"{thr}t p{q}")
            axB.axhline(0, color="0.45", lw=0.6, zorder=1)
            axB.set_xscale("symlog", base=2, linthresh=1)
            axB.set_xticks(degs); axB.set_xticklabels([str(d) for d in degs])
            axB.set_xlabel("prefetch degree $N$")
            axB.set_ylabel(r"$\Delta$ vs $N{=}0$ [\%]")
            axB.grid(True, alpha=0.25, lw=0.4, zorder=0)
            axB.legend(loc="lower left", frameon=False, handlelength=1.9,
                       labelspacing=0.22, borderaxespad=0.3)

        # ---- (f) prefetch fate ---------------------------------------------
        # Clipped to the USED fraction: a full 0-100% stack is ~90% "never
        # used" grey at every degree and shows nothing.  The point is that
        # BOTH bars shrink as degree rises -- prediction gets monotonically
        # worse -- while (c) shows runtime getting better.
        if axF is not None:
            fd = [d for d in degs if M[d]["useful"] + M[d]["wasted"]]
            fx = np.arange(len(fd))
            tot = np.array([M[d]["useful"] + M[d]["wasted"] for d in fd], float)
            tim = 100 * np.array([M[d]["useful"] - M[d]["pflate"] for d in fd]) / tot
            acc = 100 * np.array([M[d]["useful"] for d in fd]) / tot
            axF.bar(fx, tim, 0.66, color=TIMELY_C, zorder=3, label="in time")
            axF.bar(fx, acc - tim, 0.66, bottom=tim, color=LATE_C, zorder=3,
                    label="late")
            axF.set_xticks(fx); axF.set_xticklabels([str(d) for d in fd])
            axF.set_xlabel("$N$")
            axF.set_ylabel(r"prefetches used [\%]")
            axF.set_ylim(0, acc.max() * 1.34)
            axF.grid(True, axis="y", alpha=0.25, lw=0.4, zorder=0)
            axF.legend(loc="upper right", frameon=False, handlelength=1.1,
                       labelspacing=0.18, borderaxespad=0.15,
                       fontsize=a.fs - 1.7)

        # ---- (c) measured runtime decomposition ----------------------------
        # Counter-derived, no fit: sync_wb_applied_ns is the real serial stall.
        # OVERLAY, not a stack: the grey bar is the whole runtime and the blue
        # is the part of it spent stalled, so "total time" labels what it
        # actually is.  Stacking would make the grey total-minus-stall and the
        # legend would be lying.
        #
        # Normalised to the N=0 runtime, NOT to each bar's own total -- per-bar
        # normalisation pins every bar at 100% and erases the headline result
        # (84.6s -> 74.4s).  Against a fixed baseline the bar heights still
        # carry the net gain and the segments stay comparable across degrees.
        xs = np.arange(len(degs))
        base_rt = M[degs[0]]["rt"]
        total = np.array([100 * M[d]["rt"] / base_rt for d in degs])
        stall = np.array([100 * M[d]["stall"] / base_rt for d in degs])
        axC.bar(xs, total, 0.62, color=REST_C, edgecolor="#999999", lw=0.4,
                zorder=3, label="total time")
        axC.bar(xs, stall, 0.62, color=STALL_C, zorder=4,
                label="dirty write-back stall")
        axC.set_xticks(xs); axC.set_xticklabels([str(d) for d in degs])
        axC.set_xlabel("prefetch degree $N$")
        axC.set_ylabel(rf"{a.mech}-thread runtime [\% of $N{{=}}0$]")
        axC.set_ylim(0, total.max() * 1.34)
        axC.grid(True, axis="y", alpha=0.25, lw=0.4, zorder=0)

        # Right axis: absolute in-fault CLEAN evictions.  This is what the
        # growing grey block tracks, so the count -- not the share -- is the
        # quantity that explains it.
        axD = axC.twinx()
        clean = [(M[d]["sev"] - M[d]["swb"]) / 1e3 for d in degs]
        axD.plot(xs, clean, marker="D", ms=3.0, lw=1.2, color=CLEAN_C, zorder=6,
                 label="in-fault clean evictions")
        axD.set_ylabel(r"clean evictions [$\times10^3$]", color=CLEAN_C)
        axD.tick_params(axis="y", colors=CLEAN_C)
        axD.set_ylim(0, max(clean) * 1.62)

        hA, lA = axC.get_legend_handles_labels()
        hB, lB = axD.get_legend_handles_labels()
        axC.legend(hA + hB, lA + lB, loc="upper left", frameon=False,
                   handlelength=1.3, labelspacing=0.18, borderaxespad=0.35,
                   fontsize=a.fs - 2.6)

        # Bold panel letters below the x-label.  scienceplots renders text
        # through LaTeX, which ignores matplotlib's fontweight= parameter.
        if letters:
            for i, ax in enumerate(axes):
                ax.text(0.5, -0.26, rf"\textbf{{({chr(97 + i)})}}",
                        transform=ax.transAxes, ha="center", va="top",
                        fontsize=a.fs + 1)

        if ext_axes is None:
            # tight_layout's w_pad is uniform, so this tightens both gaps; the
            # freed width is redistributed to the panels by the width_ratios.
            fig.tight_layout(pad=0.35, w_pad=a.wpad)
            fig.savefig(a.out, bbox_inches="tight", dpi=400)
            fig.savefig(a.out.rsplit(".", 1)[0] + ".pdf", bbox_inches="tight")
            plt.close(fig)


def summarise(R, mech):
    M = R[mech]; degs = sorted(M); b = M[degs[0]]
    print(f"\n=== {mech}-thread runtime budget (all counter-measured) ===")
    print(f"{'N':>3}{'runtime':>9}{'stall':>8}{'rest':>8}{'dirty wb':>10}"
          f"{'clean ev':>10}{'d stall':>9}{'d rest':>8}{'d net':>8}")
    for d in degs:
        r = M[d]
        print(f"{d:>3}{r['rt']:>8.1f}s{r['stall']:>8.1f}{r['rt']-r['stall']:>8.1f}"
              f"{r['swb']:>10,}{r['sev']-r['swb']:>10,}"
              f"{r['stall']-b['stall']:>+9.1f}"
              f"{(r['rt']-r['stall'])-(b['rt']-b['stall']):>+8.1f}"
              f"{r['rt']-b['rt']:>+8.1f}")
    for thr in sorted(R):
        S = R[thr]; ds = sorted(S); z = S[ds[0]]
        note = "" if S[ds[0]]["swb"] is not None else \
               "   (pre-counter build; re-run for matched provenance)"
        print(f"\n{thr}-thread percentiles  [build {S[ds[0]]['src'][:8]}]{note}")
        print(f"{'N':>3}" + "".join(f"{f'p{q}':>8}" for q in (10, 50, 75, 90, 99))
              + f"{'mean':>8}{'d p99':>8}")
        for d in ds:
            v = S[d]["lat"]
            q = [np.percentile(v, x) for x in (10, 50, 75, 90, 99)]
            print(f"{d:>3}" + "".join(f"{x:>8.0f}" for x in q) + f"{v.mean():>8.1f}"
                  f"{100*(q[4]/np.percentile(z['lat'],99)-1):>+7.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", default="/mnt/nvme/cxdvirt/cxdvirt/reproduction/figures/"
                                     "prefetch_1t.png")
    ap.add_argument("--figsize", default="7.16,2.60")   # IEEE double column
    ap.add_argument("--fs", type=float, default=10)
    ap.add_argument("--wpad", type=float, default=0.55,
                    help="horizontal padding between panels")
    ap.add_argument("--panels", default="afc",
                    help="which panels to draw, in order (e.g. 'ac' or 'abc')")
    ap.add_argument("--inset-qlo", type=float, default=4.0,
                    help="inset x-axis starts at this percentile (default 4)")
    ap.add_argument("--inset-box", default="0.415,0.145,0.535,0.475",
                    help="8t inset [x,y,w,h] in panel-(a) axes fraction")
    ap.add_argument("--width-ratios", default="",
                    help="relative widths of panels a,b,c")
    ap.add_argument("--linmin", type=float, default=20.0)
    ap.add_argument("--linmax", type=float, default=600.0)
    ap.add_argument("--mech", type=int, default=1,
                    help="thread count for the mechanism panels (a) and (c)")
    a = ap.parse_args()

    R = collect(a.dir)
    if a.mech not in R:
        sys.exit(f"no {a.mech}-thread runs in {a.dir} (have: {sorted(R)})")
    if any(R[a.mech][d]["swb"] is None for d in R[a.mech]):
        sys.exit(f"{a.mech}-thread runs predate sync_wb_applied; panel (c) needs it")
    os.makedirs(os.path.dirname(a.out), exist_ok=True)

    import scienceplots  # noqa: F401
    try:
        build(["science", "ieee"], a, R)
    except Exception as e:
        print(f"[LaTeX render failed ({e}); no-latex fallback]", file=sys.stderr)
        build(["science", "ieee", "no-latex"], a, R)
    print(f"wrote {a.out} / .pdf")
    summarise(R, a.mech)


if __name__ == "__main__":
    main()
