#!/usr/bin/env python3
"""Eviction-policy characterisation: read-latency CDF + eviction composition.

(a) is the Cylon Figure 11 counterpart -- YCSB-C read-latency CDF at 2.67x
normalized WSS.  (b) is the mechanism behind it: how many pages each policy
evicts per read, and how many of those cost a NAND write.

BOTH panels cover the run phase only.  An earlier version of (b) plotted NAND
writes per operation for the load phase alongside the run phase, which was
misleading: the load is dataset population, not the benchmark, and it runs at
64 threads against a cold cache with a sequential-insert reference stream --
three differences from panel (a) that a shared y-axis silently implied away.
(redis-load-*.properties pins threadcount=64, so --threads does not touch it.)

The pair carries a finding neither panel shows alone.  LIFO evicts 1.96x as
often as FIFO, matching its 2x miss rate -- but nearly all of the excess is
CLEAN (1.52 vs 0.51 per op).  Its dirty evictions, the ones that actually
write NAND, are within 4% of FIFO's (0.597 vs 0.572).  So LIFO's cost inside
the benchmark is eviction volume and the stalls it causes, NOT write
amplification: it discards recently-read pages, which are less likely to be
dirty than the older load-dirtied pages FIFO and CLOCK give up.

Note that the run phase writes NAND at all only because YCSB-C is read-only to
the APPLICATION, not to the device: run-phase write-protect faults land at
~0.57 per operation, ~1 per request, consistent with Redis stamping per-object
access metadata (robj->lru) on every lookup and dirtying the page it sits on.

Colour carries policy in BOTH panels, so the mapping is learned once; in (b)
policy is additionally on the x-axis and hatch carries dirtiness, so no
identity anywhere in the figure rests on colour alone.

Sources: run stems, palette and the raw loader are imported from policy_cdf.py
so the two figures can never drift apart.  Misses/request and the eviction
split are recomputed from each run's own .ctr1/.ctr2 counter snapshots.
"""
import argparse, sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

sys.path.insert(0, "/mnt/nvme/cxdvirt/cxdvirt/reproduction/plots/tools")
from policy_cdf import COL, LS, load_raw, misses_per_request, runs, describe  # noqa: E402

ORDER = ("FIFO", "CLOCK", "LIFO")


def run_evictions(base, records):
    """-> (run-phase dirty evictions per op, clean evictions per op).

    Every dirty eviction is exactly one NAND page write, so nand_wr IS the
    dirty count; clean victims are discarded and cost nothing on the write
    path.  Both are deltas between .ctr1 (end of load) and .ctr2 (end of run),
    so this panel covers the YCSB-C measurement window ONLY -- the same window
    panel (a) plots -- and excludes dataset population entirely.
    """
    rd = lambda p: {k: int(v) for k, v in (l.split() for l in open(p))}
    c1, c2 = rd(base + ".ctr1"), rd(base + ".ctr2")
    ev = ((c2["sync_evicts"] - c1["sync_evicts"])
          + (c2["async_evicts"] - c1["async_evicts"]))
    dirty = c2["nand_wr"] - c1["nand_wr"]
    return dirty / float(records), (ev - dirty) / float(records)


def build(style, a, ext_axes=None, letters=True):
    """Draw the two policy panels.

    ext_axes -- when given, draw into these caller-supplied axes instead of
    creating a figure; the caller owns layout, panel letters and saving.  This
    is how combined_policy_prefetch.py reuses this code rather than copying it.
    """
    with plt.style.context(style):
        plt.rcParams.update({
            "font.size": a.fs, "axes.labelsize": a.fs, "axes.titlesize": a.fs,
            "xtick.labelsize": a.fs, "ytick.labelsize": a.fs,
            "legend.fontsize": a.fs - 1,
        })
        # ONE supplied axis means (b) is folded into (a) as an inset.  The CDFs
        # saturate by ~250 us of a 600 us axis, so the lower-right of the panel
        # is empty and the bars occlude nothing.  The two panels share the
        # policy colour mapping, and the bar totals ARE the miss/req numbers
        # the legend used to carry (one eviction per admission at steady
        # state), so nesting them removes a duplicated axis, not information.
        inset = ext_axes is not None and len(ext_axes) == 1
        if ext_axes is None:
            fig, (ax1, ax2) = plt.subplots(
                1, 2, figsize=tuple(float(t) for t in a.figsize.split(",")))
        elif inset:
            ax1 = ext_axes[0]
            fig = ax1.figure
            ax2 = ax1.inset_axes([float(t) for t in a.inset_box.split(",")])
        else:
            ax1, ax2 = ext_axes
            fig = ax1.figure
        # Inset type is small type: everything in (b) steps down when nested.
        bfs = (a.fs + a.inset_fs_delta) if inset else a.fs
        d, stems = runs(a)
        # scienceplots enables text.usetex, and under LaTeX a "\n" in a label is
        # not a line break -- LaTeX needs an explicit stacking environment.
        # \shortstack[l] left-aligns the two lines the way a legend wants.
        tex = plt.rcParams.get("text.usetex", False)

        # ---- (a) read-latency CDF ------------------------------------------
        rows = {}
        lat = {pol: load_raw(f"{d}/{stems[pol]}") for pol in ORDER}
        # The published range holds every reference policy to P99.9 with
        # margin; a run with longer tails extends it rather than being clipped.
        xmax = max(a.xmax, 1.5 * max(np.percentile(v, 99.9) for v in lat.values()))
        for pol in ORDER:
            stem = f"{d}/{stems[pol]}"
            mpr = misses_per_request(stem, a.records)
            v = lat[pol]
            x = np.linspace(0, xmax, 600)
            # In inset mode the miss/req figure is dropped from the legend: the
            # inset's own bar totals are the same three numbers, and one line
            # per entry is what lets the legend sit above the panel in a single
            # row instead of stealing the lower-right corner the bars now use.
            if inset:
                lbl = pol
            else:
                lbl = (r"\shortstack[l]{" + pol + r" \\ (" + f"{mpr:.2f}"
                       + r" miss/req)}") if tex else f"{pol}\n({mpr:.2f} miss/req)"
            ax1.plot(x, np.searchsorted(v, x, side="right") / v.size,
                     color=COL[pol], linestyle=LS[pol], linewidth=a.lw, zorder=3,
                     label=lbl, solid_capstyle="round", dash_capstyle="round")
            rows[pol] = (mpr, np.percentile(v, [50, 90, 99]),
                         run_evictions(stem, a.records))
        ax1.set_xlim(0, xmax)
        ax1.set_ylim(0, 1.005)
        ax1.set_xlabel(r"Redis read latency [$\mu$s]")
        ax1.set_ylabel("CDF")
        ax1.grid(True, which="major", alpha=0.25, linewidth=0.4, zorder=0)
        if inset:
            # No mode="expand".  Expanding to the axes width works only while
            # the legend's NATURAL width is smaller than the axes; once it is
            # wider -- which is what raising --fs does -- expand cannot shrink
            # the entries, so it collapses the column offsets and each handle
            # prints over the previous label.  Centred at its natural width it
            # simply overhangs the panel a little instead, into the gap before
            # (b).  Same reason (d)'s legend does not expand.
            ax1.legend(loc="lower center", bbox_to_anchor=(0.5, 1.005),
                       frameon=False, fontsize=a.legfs, ncol=3,
                       handlelength=1.5, borderaxespad=0.0,
                       columnspacing=0.9, handletextpad=0.45)
        else:
            ax1.legend(loc="lower right", frameon=False, fontsize=a.legfs,
                       handlelength=2.0, labelspacing=0.7, borderaxespad=0.3)

        # ---- (b) run-phase evictions per op, stacked dirty over clean ------
        # Stacked rather than paired: the claim is about the COMPOSITION of one
        # quantity (LIFO's extra evictions are almost entirely clean), which a
        # stack shows and side-by-side bars do not.
        xs = np.arange(len(ORDER))
        w = 0.52
        tot = {}
        for i, pol in enumerate(ORDER):
            dirty, clean = rows[pol][2]
            tot[pol] = dirty + clean
            ax2.bar(xs[i], dirty, w, color=COL[pol], edgecolor="white",
                    linewidth=0.6, zorder=3)
            ax2.bar(xs[i], clean, w, bottom=dirty, color=COL[pol], hatch="///",
                    edgecolor="white", linewidth=0.6, zorder=3)
            ax2.text(xs[i], tot[pol] + 0.035, f"{tot[pol]:.2f}", ha="center",
                     va="bottom", fontsize=bfs - 2, zorder=4)
            # dirty value inside its own segment: the panel's second point is
            # that this number is nearly constant across policies.
            ax2.text(xs[i], dirty / 2, f"{dirty:.2f}", ha="center", va="center",
                     fontsize=bfs - 3, color="white", zorder=5)
        ax2.set_xticks(xs)
        ax2.set_xticklabels(ORDER)
        ax2.set_ylim(0, max(tot.values()) * (1.30 if inset else 1.20))
        ax2.grid(True, axis="y", which="major", alpha=0.25, linewidth=0.4, zorder=0)
        # Nested, the quantity is named by a title: a rotated y-label costs
        # horizontal room the three bars need, and the long-form legend labels
        # do not fit either.  "clean"/"dirty" carry the split; the caption
        # says clean victims cost no NAND write.
        if inset:
            ax2.set_title("evictions per read op", fontsize=bfs, pad=2.5)
            ax2.tick_params(labelsize=bfs - 1, length=2.0, pad=1.5)
            for sp in ax2.spines.values():
                sp.set_linewidth(0.5)
            leg = dict(labels=("clean", "dirty"), fontsize=bfs - 1,
                       handlelength=1.0, labelspacing=0.2, borderaxespad=0.2,
                       handletextpad=0.4)
        else:
            ax2.set_ylabel("evictions per read operation")
            leg = dict(labels=("clean (no write)", "dirty (NAND write)"),
                       fontsize=a.fs - 2.5, handlelength=1.3,
                       labelspacing=0.25, borderaxespad=0.3)
        lab = leg.pop("labels")
        ax2.legend(handles=[Patch(facecolor="0.55", hatch="///",
                                  edgecolor="white", label=lab[0]),
                            Patch(facecolor="0.55", edgecolor="white",
                                  label=lab[1])],
                   loc="upper left", frameon=False, **leg)

        # ---- bold panel tags, centred below each panel, no titles ----------
        # scienceplots turns on text.usetex, and under LaTeX matplotlib's
        # fontweight= is ignored -- the weight has to be requested in the TeX
        # string itself.  Fall back to fontweight for the no-latex style.
        #
        # Placed with fig.text, not ax.text: panel (a) carries an xlabel and
        # panel (b) does not, so a fixed offset in axes coordinates would put
        # the two tags at different heights.  Reserving a strip with
        # tight_layout(rect=...) and addressing it in FIGURE coordinates puts
        # both on one baseline, each centred on its own axes.
        if ext_axes is None:
            fig.tight_layout(pad=0.4, w_pad=1.6, rect=(0, a.taggap, 1, 1))
            if letters:
                for ax, tag in ((ax1, "a"), (ax2, "b")):
                    box = ax.get_position()
                    fig.text(box.x0 + box.width / 2, 0.004,
                             rf"\textbf{{({tag})}}" if tex else f"({tag})",
                             ha="center", va="bottom",
                             fontweight=None if tex else "bold", fontsize=a.fs)
            fig.savefig(a.out, bbox_inches="tight", dpi=400)
            fig.savefig(a.out.rsplit(".", 1)[0] + ".pdf", bbox_inches="tight")
            plt.close(fig)
            print("wrote", a.out)

    print(f"  {describe(a)}")
    for pol in ORDER:
        mpr, (p50, p90, p99), (dirty, clean) = rows[pol]
        print(f"  {pol:<6} p50 {p50:6.0f}  p90 {p90:6.0f}  p99 {p99:6.0f}   "
              f"{mpr:.3f} miss/req   evict/op {dirty + clean:.3f} "
              f"= dirty {dirty:.3f} + clean {clean:.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drain", type=int, choices=[0, 1, 2], default=2)
    # Run-phase client threads; the load phase is 64 either way.
    ap.add_argument("--threads", type=int, choices=[1, 8], default=1)
    ap.add_argument("--out", default=None)
    ap.add_argument("--figsize", default="7.2,2.9")
    ap.add_argument("--fs", type=float, default=12)
    ap.add_argument("--lw", type=float, default=2.0,
                    help="CDF line width in points")
    ap.add_argument("--legfs", type=float, default=11,
                    help="legend font size for panel (a)")
    ap.add_argument("--xmax", type=float, default=0,
                    help="0 = auto: 600 us at 1 thread, 2000 at 8")
    ap.add_argument("--records", type=int, default=1000000)
    ap.add_argument("--policy-dir", dest="policy_dir", default=None,
                    help="plot the runs found in this directory instead of the "
                         "published stems (policy_cdf.discover)")
    ap.add_argument("--regime", choices=["slow", "fast"], default="fast")
    ap.add_argument("--inset-box", dest="inset_box",
                    default="0.44,0.12,0.545,0.60",
                    help="x0,y0,w,h in axes fractions for (b) when it is drawn "
                         "as an inset of (a) (combined figure only)")
    ap.add_argument("--inset-fs-delta", dest="inset_fs_delta", type=float,
                    default=-2.5, help="font-size offset from --fs inside the "
                                       "nested (b)")
    ap.add_argument("--taggap", type=float, default=0.03,
                    help="figure-height fraction reserved below the axes for "
                         "the (a)/(b) tags; smaller pulls them closer")
    a = ap.parse_args()
    if not a.xmax:
        a.xmax = 600.0 if a.threads == 1 else 2000.0
    if a.out is None:
        a.out = ("/mnt/nvme/cxdvirt/cxdvirt/reproduction/figures/"
                 f"redis_policy_cdf_evict_267wss_drain{a.drain}"
                 f"_{a.threads}t.png")
    import scienceplots  # noqa: F401
    try:
        build(["science", "ieee"], a)
    except Exception as e:
        print(f"[latex failed ({e})]", file=sys.stderr)
        build(["science", "ieee", "no-latex"], a)


if __name__ == "__main__":
    main()
