#!/usr/bin/env python3
"""Eviction policy (top) and prefetch mechanism (bottom) in one figure.

Row 1 -- (a) Redis read-latency CDF by eviction policy at 2.67x normalized WSS,
             with evictions-per-read composition nested as an inset,
         (b) 1-thread read-latency CDF by prefetch degree, fast-eviction arm,
             with the slow-eviction arm nested as an inset.
Row 2 -- (c) prefetch fate in both regimes, (d) slow-arm runtime decomposition.

Both panels of the policy pair share a colour mapping and the CDF saturates by
~250 us of a 600 us axis, so the bars are nested rather than given their own
slot.  Nothing is lost by it: the bar totals ARE the miss/req figures the (a)
legend used to spell out, since at steady-state occupancy every admission
displaces exactly one page.  Freeing that slot is what lets the prefetch CDF
move up beside it, which in turn lets the last two panels split a whole row
instead of two thirds of one.

Nothing is drawn here.  Both rows call the SAME build() the standalone figures
use, via their ext_axes hook, so the combined figure cannot drift from
policy_combined.py or plot_prefetch_regimes.py.  This file owns only the grid,
the panel letters and the save.

The two rows deliberately keep their own palettes.  The top row's colours mean
EVICTION POLICY (Tol high-contrast) and the bottom row's mean PREFETCH DEGREE
(Tol bright, walked cool->warm as an ordered ramp).  They are different
variables, so sharing a mapping would be the error, not the fix; each row
carries its own legend and the letters mark the boundary.
"""
import argparse, os, sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/mnt/nvme/cxdvirt/cxdvirt/reproduction")
import policy_combined as P                                     # noqa: E402
import plot_prefetch_regimes as F                              # noqa: E402


class NS(dict):
    """argparse-like view over a dict, so the imported builds see the knobs
    they expect without either script needing to change its signature."""
    __getattr__ = dict.get


def build(style, a, R):
    with plt.style.context(style):
        plt.rcParams.update({
            "font.size": a.fs, "axes.labelsize": a.fs, "axes.titlesize": a.fs,
            "xtick.labelsize": a.fs, "ytick.labelsize": a.fs,
            "legend.fontsize": a.fs - 1,
        })
        w, h = (float(t) for t in a.figsize.split(","))
        fig = plt.figure(figsize=(w, h))
        gs = fig.add_gridspec(2, 1, height_ratios=[float(t) for t in
                                                   a.row_ratios.split(",")],
                              hspace=a.hspace)
        # Two rows of two.  The eviction-composition bars ride as an inset of
        # the policy CDF (they share its colour mapping and its lower-right is
        # empty), which frees a top-row slot for the prefetch CDF; the two
        # remaining prefetch panels then split a full row instead of a third of
        # one, which is where their size comes from.
        g0 = gs[0].subgridspec(1, 2, width_ratios=[float(t) for t in
                                                   a.wr0.split(",")],
                               wspace=a.wspace0)
        g1 = gs[1].subgridspec(1, 2, width_ratios=[float(t) for t in
                                                   a.wr1.split(",")],
                               wspace=a.wspace1)
        top = [fig.add_subplot(g0[i]) for i in range(2)]
        bot = [fig.add_subplot(g1[i]) for i in range(2)]

        # (a) alone -> policy_combined nests its second panel as an inset.
        P.build(style, NS(drain=a.drain, threads=a.threads,
                          records=a.records, xmax=a.xmax,
                          policy_dir=a.policy_dir, regime=a.regime,
                          fs=a.fs, lw=a.lw, legfs=a.legfs, annotate=True,
                          inset_box=a.pol_inset_box,
                          inset_fs_delta=a.pol_inset_fs_delta),
                ext_axes=[top[0]], letters=False)
        # (b) top-right, (c)/(d) across the bottom row.
        F.build(style, NS(fs=a.fs, figsize="1,1", width_ratios="", wpad=a.wpad,
                          inset_qlo=a.inset_qlo, inset_box=a.inset_box,
                          linmin=a.linmin, linmax=a.linmax,
                          legfs_c=a.legfs_c, legfs_d=a.legfs_d,
                          legfs_e=a.legfs_e, legd_raise=a.legd_raise,
                          ins_title_fs=a.ins_title_fs, xlo_pad=a.xlo_pad),
                R, ext_axes=[top[1], bot[0], bot[1]], letters=False)

        # (b) has no right-hand y-axis, but (d) directly below it does, so the
        # figure's tight right edge is set by (d)'s twin label and (b) stops
        # well short of it -- a visible notch of dead space in the top-right.
        # Stretch (b) out to that edge.  Measured rather than hard-coded: the
        # gap depends on the twin's tick widths, which depend on --fs.  Its
        # inset and its expanding legend are in axes coordinates, so both
        # follow.  Safe because this file never calls tight_layout.
        # bbox_extra_artists=[] excludes LEGENDS from both measurements.  With
        # them included the comparison is meaningless: (b)'s legend overhangs
        # its own panel, so its tight bbox already reaches (d)'s edge and the
        # computed growth collapses to ~0 -- the stretch silently stops
        # happening.  Axis labels and tick labels are the axes' own artists and
        # are still counted, which is what we want to align.
        fig.canvas.draw()
        want = max(ax.get_tightbbox(bbox_extra_artists=[]).x1 for ax in fig.axes)
        have = top[1].get_tightbbox(bbox_extra_artists=[]).x1
        if want > have:
            inv = fig.transFigure.inverted()
            grow = (inv.transform((want, 0))[0]
                    - inv.transform((have, 0))[0]) - a.b_right_pad
            p = top[1].get_position()
            if grow > 0:
                top[1].set_position([p.x0, p.y0, p.width + grow, p.height])

        # Panel letters, in reading order across rows.
        #
        # Placed in FIGURE coordinates on a per-ROW baseline, not at a fixed
        # axes fraction.  The axes-fraction form (ax.text at a constant --tagy)
        # only looks aligned while every panel in a row hangs the same distance
        # below its axes; it drifts the moment two panels differ in what sits
        # under them -- an x-label on one and not the other, a wrapped x-label,
        # taller tick labels -- and it means something different in each row,
        # since the rows have different heights.  Measuring each row's lowest
        # tight edge (which includes tick labels and x-label) and offsetting a
        # fixed figure fraction from THAT puts the row's letters on one line
        # and keeps both rows' letters the same distance from their panels.
        #
        # Must run after the (b) stretch above, and re-draw, because that moved
        # an axes; and before the letters exist, so they cannot feed back into
        # the tight bounds being measured.
        tex = plt.rcParams.get("text.usetex", False)
        fig.canvas.draw()
        inv = fig.transFigure.inverted()
        n = 0
        for row in (top, bot):
            base = min(inv.transform((0, ax.get_tightbbox().y0))[1] for ax in row)
            for ax in row:
                box = ax.get_position()
                fig.text(box.x0 + box.width / 2, base - a.taggap,
                         (rf"\textbf{{({chr(97 + n)})}}" if tex
                          else f"({chr(97 + n)})"),
                         ha="center", va="top", fontsize=a.fs + 1)
                n += 1

        fig.savefig(a.out, bbox_inches="tight", dpi=400)
        fig.savefig(a.out.rsplit(".", 1)[0] + ".pdf", bbox_inches="tight")
        plt.close(fig)
    print("wrote", a.out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="/mnt/nvme/cxdvirt/cxdvirt/reproduction/results/redis/fig8_4914/prefetch",
                    help="prefetch sweep directory for the bottom row")
    ap.add_argument("--out", default=None)
    ap.add_argument("--figsize", default="7.16,5.4")
    ap.add_argument("--row-ratios", dest="row_ratios", default="1.0,0.94")
    # hspace is a FRACTION of panel height, so it does not scale with
    # --figsize: shrinking the figure needs a LARGER fraction for the same
    # absolute gap.  The binding constraint is that the (a)/(b) letters hang
    # BELOW their x-labels while (c)'s two-row legend sits ABOVE its axes, so
    # the row gap has to clear both at once; under ~0.60 they collide.
    ap.add_argument("--hspace", type=float, default=0.74)
    ap.add_argument("--wspace0", type=float, default=0.34)
    ap.add_argument("--wspace1", type=float, default=0.42)
    # Bottom-row width ratios.  (d) carries a twin axis whose label and tick
    # numbers live outside its right spine, and a three-entry legend whose
    # longest string is "in-fault clean evictions"; (c) needs only enough width
    # for four bar pairs and a two-column legend.  Hence the tilt toward (d).
    ap.add_argument("--wr0", default="1.0,1.0",
                    help="width ratios for the top row (a,b)")
    ap.add_argument("--wr1", default="0.84,1.16",
                    help="width ratios for the bottom row (c,d).  (d) carries a "
                         "twin axis with its own label, so it is given the "
                         "wider slot")
    ap.add_argument("--pol-inset-box", dest="pol_inset_box",
                    default="0.30,0.13,0.65,0.665",
                    help="the eviction bars' box inside (a)")
    ap.add_argument("--pol-inset-fs-delta", dest="pol_inset_fs_delta",
                    type=float, default=-2.5)
    ap.add_argument("--b-right-pad", dest="b_right_pad", type=float,
                    default=0.03,
                    help="figure-width fraction of whitespace to leave to the "
                         "right of (b) when it is stretched out to (d)'s edge")
    ap.add_argument("--ins-title-fs", dest="ins_title_fs", type=float,
                    default=None,
                    help="(b) inset title size; default --fs minus 3.5.  Too "
                         "large and the centred title runs off the inset and "
                         "over its own y-tick labels")
    ap.add_argument("--xlo-pad", dest="xlo_pad", type=float, default=1.18,
                    help="(b) x-axis is opened this factor below its first "
                         "tick so that tick's label clears the y-axis 0.0")
    ap.add_argument("--legd-raise", dest="legd_raise", type=float, default=None,
                    help="raisebox factor for (d)'s two-line legend labels; "
                         "0 puts the handle on the second line, -0.5 on the "
                         "first, default -0.25 between them")
    ap.add_argument("--legfs-c", dest="legfs_c", type=float, default=None,
                    help="(b) degree-legend size; default --fs minus 2.6")
    ap.add_argument("--legfs-d", dest="legfs_d", type=float, default=None)
    ap.add_argument("--legfs-e", dest="legfs_e", type=float, default=None)
    ap.add_argument("--taggap", type=float, default=0.006,
                    help="figure-height fraction between a row's lowest tight "
                         "edge and its panel letters")
    ap.add_argument("--fs", type=float, default=14)
    # top row
    ap.add_argument("--drain", type=int, choices=[0, 1, 2], default=2)
    # Run-phase client threads for the POLICY row.  Default 1 matches the
    # prefetch row, which plot_prefetch_1t.py fixes at one thread via --mech,
    # so all five panels share a concurrency.  It also matches Cylon's
    # run-figure11.sh (THREADS=1), whose paper text says 8.  Use 8 for the
    # deployment-realistic case.
    ap.add_argument("--threads", type=int, choices=[1, 8], default=1)
    ap.add_argument("--records", type=int, default=1000000)
    # A re-collected Fig. 8 (run_experiment.sh fig8a-policy / fig8bcd-prefetch)
    # is plotted with --policy-dir <its policy runs> --dir <its prefetch sweep>
    # --records <its record count>; without --policy-dir the top row is the
    # published 614 MB / 1M-record stems in policy_cdf.DATA.
    ap.add_argument("--policy-dir", dest="policy_dir", default=None,
                    help="policy runs for (a), found by policy_cdf.discover")
    ap.add_argument("--regime", choices=["slow", "fast"], default="fast",
                    help="drain regime of the (a) runs taken from --policy-dir")
    ap.add_argument("--xmax", type=float, default=0,
                    help="0 = auto: 600 us at 1 thread, 2000 at 8")
    ap.add_argument("--lw", type=float, default=1.6)
    ap.add_argument("--legfs", type=float, default=None,
                    help="(a) policy-legend size; default --fs minus 3")
    # bottom row (defaults mirror plot_prefetch_1t.py)
    ap.add_argument("--mech", type=int, default=1)
    ap.add_argument("--inset-qlo", dest="inset_qlo", type=float, default=4.0)
    ap.add_argument("--inset-box", dest="inset_box",
                    default="0.415,0.145,0.535,0.475")
    ap.add_argument("--linmin", type=float, default=20.0)
    ap.add_argument("--linmax", type=float, default=600.0)
    ap.add_argument("--wpad", type=float, default=0.55)
    a = ap.parse_args()
    # Every legend size is an offset from --fs so that raising the base size
    # raises the whole figure's type, rather than leaving the legends behind.
    if a.legfs is None:
        a.legfs = a.fs - 3.0
    if not a.xmax:
        a.xmax = 600.0 if a.threads == 1 else 2000.0
    if a.out is None:
        a.out = ("/mnt/nvme/cxdvirt/cxdvirt/reproduction/figures/"
                 f"policy_prefetch_combined_{a.threads}t.png")

    R = F.collect(a.dir)
    import scienceplots  # noqa: F401
    try:
        build(["science", "ieee"], a, R)
    except Exception as e:
        print(f"[latex failed ({e})]", file=sys.stderr)
        build(["science", "ieee", "no-latex"], a, R)


if __name__ == "__main__":
    main()
