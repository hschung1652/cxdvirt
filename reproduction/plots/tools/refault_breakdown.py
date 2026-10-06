#!/usr/bin/env python3
"""refault_breakdown.py — per-percentile felt per-miss latency, tail DECOMPOSED.

  KVM/QEMU/FTL/NAND   = genuine per-FILL device servicing (full_*.csv p50 band),
                        held CONSTANT (flat body).
  primary-fill stall  = the demand fill's host window inflating while the vCPU
                        spins in wait_for_buf_update on an FTL poller busy evicting.
  multi-exit refault  = EMULATION_OK_RESTART re-execution round-trips until the
                        SPTE Direct-flip lands (~3us each, no NAND re-paid).
  re-entry gap        = felt - sum(host windows): resume / TLB / inter-bounce time
                        (its p50 value is the flat ~0.9us VM-transition floor).

Bars reach the POINT percentile of guest-felt latency (true quantile).  The three
tail components are measured as band-means (exact guest-TSC join) and only set the
tail COMPOSITION — they are rescaled so their sum equals the point-percentile tail
residual (felt_point - servicing).  So the bar TOTAL is an exact quantile while the
split is measured.  Faceted per config (own x-scale).

Modes (argv[1]):
  (none)   100%-miss workload (bench_4096_tsc captures)      -> breakdown_3way_refault_*.png
  50miss   50%-miss workload  (bench_2048_tsc2, misses only) -> *_50miss.png
  cmp      BOTH, grouped bars per percentile; 50%-miss hatched -> *_cmp.png"""
import numpy as np, sys, os
# ns of channel transfer the capture was configured with (ch_xfer_lat).
# Stock Cylon is 0 (its channel stage is #if 0'd upstream); the parity
# captures use 3232 = 4 KiB at 1200 MiB/s.  >0 splits it out of FTL+NAND.
CH_US = float(os.environ.get("CH_XFER", "0")) / 1e3
MODE = sys.argv[1] if len(sys.argv) > 1 else ""
TSC=2.1; PCTS=[50,90,99,99.9,99.99]
# RB_CFG env override, e.g. RB_CFG="3us:znand3us_parity,40us:stock40us_parity"
# (label:tag pairs).  Default is the original three stock configs.
import os as _os
CFG=[tuple(p.split(":")) for p in _os.environ["RB_CFG"].split(",")] \
    if _os.environ.get("RB_CFG") else \
    [("nolat","h0"),("3us","znand3us"),("40us","stock40us")]
# display labels (config keys stay ASCII; figures render microseconds properly)
LABEL={"nolat":"nolat","3us":r"3 $\mu$s","40us":r"40 $\mu$s"}
SERV=["KVM","QEMU","FTL","NAND"]
# Exit windows of the benchmark's own vCPU only.  kvm_optb logs every vCPU's
# MMIO exits, and the containment join used to add any window that fell inside
# an access -- so other vCPUs' concurrent exits were counted as multi-exit
# refaults of the measured access (22-27 us of the 40 us p99.9 bar, 3-8 us at
# 3 us), and whatever time they covered vanished from the re-entry gap.  The
# captures are single-threaded, so the benchmark's vCPU is the one with the
# most windows.  RB_ALL_VCPUS=1 restores the old all-vCPU join.
ALL_VCPUS=os.environ.get("RB_ALL_VCPUS")=="1"
def own_windows(k):
    gx=k["gtsc_exit"].astype(np.int64); gn=k["gtsc_entry"].astype(np.int64)
    if ALL_VCPUS: return gx,gn
    v=k["vcpu"].astype(int); keep=v==np.bincount(v).argmax()
    return gx[keep],gn[keep]
TAIL=["primary-fill stall","multi-exit refault","re-entry gap"]
STAGES=SERV+TAIL
# CYLON_DATA points at another capture directory -- make_figures.sh passes the
# artifact's results/cylon, so a re-collection is drawn instead of the reference.
D=os.environ.get("CYLON_DATA") or "/mnt/nvme/cxdvirt/cxdvirt/reproduction/results/cylon"
SUFS={"100% miss":("_seq","_gtsc","_gtsc"), "50% miss":("_2048","_2048","_2048")}
# per-config capture override for the 50%-miss dataset (rerun selection):
# nolat uses run 2 (fast-mode draw; run 1 drew the slow handoff mode)
RUN_PICK={"h0":"_2048r2","znand3us":"_2048r2"}
def suf50(tag,s):
    return RUN_PICK.get(tag,s) if s=="_2048" else s

def _trace(path):
    # the trace itself, or its compressed copy (numpy reads .xz and .gz as it
    # reads plain text)
    for p in (path,path+".xz",path+".gz"):
        if os.path.exists(p): return p
    return path

def band(order,N,P,frac=0.001):
    # half-width 0.1% of N, but at most half the distance to the top, so the
    # band stays centred on P (same as felt_cmp_plot.band).  Unclamped, p99.9
    # and p99.99 averaged everything up to the slowest access.
    half=max(1,int(min(frac,(1-P/100)/2)*N)); r=int(round(P/100*(N-1)))
    return order[max(0,r-half):min(N,r+half+1)]

def base_servicing(tag,sufs):
    """flat body: p50-band stage split of the host window (KVM/QEMU/FTL/NAND)."""
    d=np.genfromtxt(_trace(f"{D}/full_{tag}{suf50(tag,sufs[0])}.csv"),delimiter=",",names=True)
    tot=d["total"]; sel=band(np.argsort(tot),len(tot),50)
    KVM=d["KVM"][sel].mean(); QEMU=d["QEMU"][sel].mean()
    FTL=(d["inbound"]+d["service"]+d["outbound"])[sel].mean()
    NAND=d["modeled"][sel].mean() if "modeled" in d.dtype.names \
         else max(0,tot[sel].mean()-(KVM+QEMU+FTL))
    return np.array([KVM,QEMU,FTL,NAND])/1e3

def join_access(tag,sufs):
    """per matched access (exact guest-TSC containment join): felt, first-window,
    sum-of-windows [us].  Matched accesses = misses (hits contain no window)."""
    gt=np.loadtxt(_trace(f"{D}/guestfelt_{tag}{suf50(tag,sufs[1])}.txt"))
    ts,dc=gt[:,0].astype(np.int64),gt[:,1].astype(np.int64)
    k=np.genfromtxt(_trace(f"{D}/kvmwin_{tag}{suf50(tag,sufs[2])}.csv"),delimiter=",",names=True)
    gx,gn=own_windows(k); win=gn-gx
    Ng=len(ts); ge=ts+dc; order=np.argsort(ts)
    idx=np.searchsorted(ts[order],gx,side="right")-1
    ok=(idx>=0)&(idx<Ng); j=order[np.clip(idx,0,Ng-1)]
    c=ok&(gx>=ts[j])&(gn<=ge[j])                     # FULL window inside the access
    ci=np.where(c)[0]; acc=j[ci]
    ow=np.lexsort((gx[ci],acc)); ci=ci[ow]; acc=acc[ow]  # windows in time order per access
    fmask=np.r_[True,acc[1:]!=acc[:-1]]              # first window of each access
    fw=np.zeros(Ng,np.int64); sw=np.zeros(Ng,np.int64); cnt=np.zeros(Ng,int)
    fw[acc[fmask]]=win[ci[fmask]]; np.add.at(sw,acc,win[ci]); np.add.at(cnt,acc,1)
    have=cnt>0
    return dc[have]/TSC/1e3, fw[have]/TSC/1e3, sw[have]/TSC/1e3

def _windows(tag,sufs):
    """join_access()'s per-access window structure, but returning FULL-LENGTH
    arrays plus the coverage mask so it can be aligned with the stage join."""
    gt=np.loadtxt(_trace(f"{D}/guestfelt_{tag}{suf50(tag,sufs[1])}.txt"))
    ts,dc=gt[:,0].astype(np.int64),gt[:,1].astype(np.int64)
    k=np.genfromtxt(_trace(f"{D}/kvmwin_{tag}{suf50(tag,sufs[2])}.csv"),delimiter=",",names=True)
    gx,gn=own_windows(k); win=gn-gx
    Ng=len(ts); ge=ts+dc; order=np.argsort(ts)
    idx=np.searchsorted(ts[order],gx,side="right")-1
    ok=(idx>=0)&(idx<Ng); j=order[np.clip(idx,0,Ng-1)]
    c=ok&(gx>=ts[j])&(gn<=ge[j])
    ci=np.where(c)[0]; a=j[ci]
    ow=np.lexsort((gx[ci],a)); ci=ci[ow]; a=a[ow]
    fmask=np.r_[True,a[1:]!=a[:-1]]
    fw=np.zeros(Ng,np.int64); sw=np.zeros(Ng,np.int64); cnt=np.zeros(Ng,int)
    fw[a[fmask]]=win[ci[fmask]]; np.add.at(sw,a,win[ci]); np.add.at(cnt,a,1)
    return dc/TSC/1e3, fw/TSC/1e3, sw/TSC/1e3, cnt>0


def compute_measured(sufs):
    """Per-percentile MEASURED decomposition.

    Requires full_<tag><suf>.csv to carry a `gtsc_exit` column (optb_kvm_join.py
    emits it once the capture pairs the stage dump with the same run's host
    windows).  Each stage row is attached to the guest ACCESS whose
    [tsc, tsc+cycles] window contains its gtsc_exit; the body is then the
    band-mean of the real stages at each percentile instead of a p50 constant,
    and the tail terms keep their raw band-means instead of being rescaled to
    close on the quantile.

    The bars therefore no longer sum to the quantile, and that residual is the
    output that matters: it is how much of the felt tail the join fails to
    explain, which compute()'s rescale forces to zero by construction."""
    data={}; resid={}
    for name,tag in CFG:
        # measured mode requires the stage dump and the guest-felt log to come
        # from the SAME run, so the full_ file takes the guestfelt suffix
        # (sufs[1]) rather than the legacy sufs[0]=_seq of the modelled path.
        d=np.genfromtxt(_trace(f"{D}/full_{tag}{suf50(tag,sufs[1])}.csv"),delimiter=",",names=True)
        if "gtsc_exit" not in d.dtype.names:
            raise SystemExit(f"full_{tag}{suf50(tag,sufs[1])}.csv has no gtsc_exit column — "
                             "recapture with the patched optb_kvm_join.py")
        gt=np.loadtxt(_trace(f"{D}/guestfelt_{tag}{suf50(tag,sufs[1])}.txt"))
        ts,dc=gt[:,0].astype(np.int64),gt[:,1].astype(np.int64)
        ge=ts+dc; order=np.argsort(ts); Ng=len(ts)
        gx=d["gtsc_exit"].astype(np.int64)
        idx=np.searchsorted(ts[order],gx,side="right")-1
        ok=(idx>=0)&(idx<Ng); j=order[np.clip(idx,0,Ng-1)]
        c=ok&(gx>=ts[j])&(gx<=ge[j])                    # stage row inside the access
        acc=j[c]
        KVM=d["KVM"][c]/1e3; QEMU=d["QEMU"][c]/1e3
        FTL=(d["inbound"]+d["service"]+d["outbound"])[c]/1e3
        NAND=(d["modeled"][c]/1e3 if "modeled" in d.dtype.names else np.zeros(c.sum()))
        # per-ACCESS stage totals (an access may own >1 stage record)
        SK=np.zeros(Ng); SQ=np.zeros(Ng); SF=np.zeros(Ng); SN=np.zeros(Ng)
        np.add.at(SK,acc,KVM); np.add.at(SQ,acc,QEMU)
        np.add.at(SF,acc,FTL); np.add.at(SN,acc,NAND)

        # window structure for the SAME accesses, so the three tail terms keep
        # the definitions the modelled path uses: the first window inflating
        # beyond its own recorded servicing (primary-fill stall), the extra
        # windows beyond the first (multi-exit refault), and the felt time that
        # lies outside every window (re-entry gap).
        felt_all,fw_all,sw_all,have=_windows(tag,sufs)
        sel=have&(np.bincount(acc,minlength=Ng)>0)     # accesses with BOTH
        felt=felt_all[sel]
        body=(SK+SQ+SF+SN)[sel]
        first=fw_all[sel]; swin=sw_all[sel]
        o=np.argsort(felt); N=len(felt); rows=[]; rs=[]
        print(f"  [measured] {name}: {c.sum():,}/{len(gx):,} stage rows joined; "
              f"{sel.sum():,} accesses have both stages and windows", file=sys.stderr)
        for P in PCTS:
            b=band(o,N,P)
            kv,qe,ft,nd=SK[sel][b].mean(),SQ[sel][b].mean(),SF[sel][b].mean(),SN[sel][b].mean()
            fillstall=max(0.0,(first-body)[b].mean())
            refault  =max(0.0,(swin-first)[b].mean())
            gap      =max(0.0,(felt-swin)[b].mean())
            rows.append([kv,qe,ft,nd,fillstall,refault,gap])
            rs.append(np.percentile(felt,P)-(kv+qe+ft+nd+fillstall+refault+gap))
        data[name]=np.array(rows); resid[name]=np.array(rs)
    return data,resid


def compute(sufs):
    """-> {config: rows[pct][stage]}  (rows sum to the felt point percentile)."""
    data={}
    for name,tag in CFG:
        svc=base_servicing(tag,sufs); felt,first,swin=join_access(tag,sufs)
        body=svc.sum(); o=np.argsort(felt); N=len(felt); rows=[]
        for P in PCTS:
            s=band(o,N,P)
            fillstall=max(0.0, first[s].mean()-body)      # band-mean composition
            refault  =max(0.0, (swin-first)[s].mean())
            gap      =max(0.0, (felt-swin)[s].mean())
            tail_bm=fillstall+refault+gap
            tail_pt=max(0.0, np.percentile(felt,P)-body)
            if tail_bm>0:
                k=tail_pt/tail_bm; fillstall*=k; refault*=k; gap*=k
            rows.append(list(svc)+[fillstall,refault,gap])
        data[name]=np.array(rows)
    return data

def report(label,data):
    for name,_ in CFG:
        print(f"\n=== {name} [{label}] [us] ===")
        print("  pP    "+"".join(f"{s[:12]:>14}" for s in STAGES)+f"{'TOTAL':>9}")
        for i,P in enumerate(PCTS):
            v=data[name][i]; print(f"  p{P:<5}"+"".join(f"{x:>14.2f}" for x in v)+f"{v.sum():>9.2f}")

def draw(datasets,out_suf):
    """datasets: list of (style_label, data, hatch)."""
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt, matplotlib.patches as mpatches
        import scienceplots  # noqa
    except Exception as e:
        print(f"[plot skipped: {e}]",file=sys.stderr); return
    def savefig(f,p,**k): f.savefig(p,bbox_inches="tight",**k); f.savefig(p[:-4]+".pdf",bbox_inches="tight",**k)
    color={s:plt.get_cmap("tab10")(i) for i,s in enumerate(SERV)}
    color["primary-fill stall"]="#8c6bb1"   # purple
    color["multi-exit refault"]="#74add1"   # blue
    color["re-entry gap"]="0.6"             # gray
    # FTL+NAND merged: within the deadline the two are complementary (machinery
    # under the anchor vs delivered spin), so their split is a mode artifact.
    color["FTL+NAND"]=plt.get_cmap("tab10")(2)  # green
    PSERV=["KVM","QEMU","FTL+NAND"]; PSTAGES=PSERV+TAIL
    def seg(data,name,s):
        if s=="FTL+NAND":
            return data[name][:,STAGES.index("FTL")]+data[name][:,STAGES.index("NAND")]
        return data[name][:,STAGES.index(s)]
    y=np.arange(len(PCTS)); nd=len(datasets)
    hgt=0.8 if nd==1 else 0.38
    offs=[0.0] if nd==1 else [+0.21,-0.21]   # first dataset upper, second lower
    def patches(names): return [mpatches.Patch(facecolor=color[s],edgecolor="black",linewidth=0.5,label=s) for s in names]
    def style_patches():
        import matplotlib.pyplot as _plt
        esc=(lambda s: s.replace("%",r"\%")) if _plt.rcParams.get("text.usetex") else (lambda s: s)
        return [mpatches.Patch(facecolor="white",edgecolor="black",linewidth=0.5,
                               hatch=h if h else None,label=esc(l)) for l,_,h in datasets]
    def panel(ax,name,fs_title):
        for (label,data,hatch),off in zip(datasets,offs):
            base=np.zeros(len(PCTS))
            for s in PSTAGES:
                vals=seg(data,name,s)
                ax.barh(y+off,vals,height=hgt,left=base,color=color[s],
                        edgecolor="black",linewidth=0.4,hatch=hatch)
                base+=vals
        ax.set_title(LABEL.get(name,name),loc="left",fontsize=fs_title,pad=3)
        ax.grid(False); ax.margins(y=0.06); ax.set_xlabel(r"felt per-miss latency ($\mu$s)")

    # --- tall 3x1 (screen / column figure) ---
    with plt.style.context(["science","ieee"]):
        plt.rcParams.update({"font.size":16,"axes.labelsize":18,"xtick.labelsize":15,
                             "ytick.labelsize":16,"legend.fontsize":14})
        nc=len(CFG)
        fig,axs=plt.subplots(nc,1,figsize=(8.0,(3.0 if nd==1 else 3.5)*nc))
        axs=np.atleast_1d(axs)
        for ax,(name,_) in zip(axs,CFG):
            panel(ax,name,17)
            ax.set_yticks(y); ax.set_yticklabels([f"p{P:g}" for P in PCTS])
        H=fig.get_figheight(); rows=1.02 if nd==1 else 1.30; top=1-rows/H
        fig.tight_layout(rect=(0,0,1,top))
        fig.legend(handles=patches(PSERV),ncol=3,loc="upper center",bbox_to_anchor=(0.5,1-0.34/H))
        fig.legend(handles=patches(TAIL),ncol=3,loc="upper center",bbox_to_anchor=(0.5,1-0.62/H))
        if nd>1:
            fig.legend(handles=style_patches(),ncol=nd,loc="upper center",bbox_to_anchor=(0.5,1-0.90/H))
        out=f"/mnt/nvme/cxdvirt/cxdvirt/reproduction/figures/breakdown_3way_refault_seq{out_suf}.png"
        savefig(fig,out,dpi=300); plt.close(fig); print(f"wrote {out} (+.pdf)")

    # --- wide 1x3 side-by-side (full-page landscape paper figure) ---
    with plt.style.context(["science","ieee"]):
        plt.rcParams.update({"font.size":13,"axes.labelsize":14,"xtick.labelsize":11,
                             "ytick.labelsize":13,"legend.fontsize":12})
        nc=len(CFG)
        fig,axs=plt.subplots(1,nc,figsize=(4.0*nc,3.5 if nd==1 else 4.3),sharey=True)
        axs=np.atleast_1d(axs)
        for ax,(name,_) in zip(axs,CFG):
            panel(ax,name,14)
        axs[0].set_yticks(y); axs[0].set_yticklabels([f"p{P:g}" for P in PCTS])
        H=fig.get_figheight(); strip=0.42 if nd==1 else 0.70; top=1-strip/H
        fig.tight_layout(rect=(0,0,1,top))
        fig.legend(handles=patches(PSTAGES),ncol=6,loc="upper center",bbox_to_anchor=(0.5,1-0.06/H),
                   columnspacing=1.1,handletextpad=0.5)
        if nd>1:
            fig.legend(handles=style_patches(),ncol=nd,loc="upper center",bbox_to_anchor=(0.5,1-0.36/H),
                       columnspacing=1.4,handletextpad=0.6)
        out=f"/mnt/nvme/cxdvirt/cxdvirt/reproduction/figures/breakdown_3way_refault_horiz{out_suf}.png"
        savefig(fig,out,dpi=300); plt.close(fig); print(f"wrote {out} (+.pdf)")

def draw_cfgs(data,names,out_suf,hatches=(None,"///")):
    """Single panel, TWO configs grouped per percentile (hatch-differentiated).
    Shared x-axis, so the two configs' magnitudes are directly comparable."""
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt, matplotlib.patches as mpatches
        import scienceplots  # noqa
    except Exception as e:
        print(f"[plot skipped: {e}]",file=sys.stderr); return
    def savefig(f,p,**k): f.savefig(p,bbox_inches="tight",**k); f.savefig(p[:-4]+".pdf",bbox_inches="tight",**k)
    # Paul Tol bright, on the same semantic slots the tab10 version used
    color={"KVM":"#4477AA","QEMU":"#EE6677","FTL+NAND":"#228833",
           "channel transfer":"#CCBB44","primary-fill stall":"#AA3377",
           "multi-exit refault":"#66CCEE","re-entry gap":"#BBBBBB"}
    PSERV=["KVM","QEMU","FTL+NAND"]+(["channel transfer"] if CH_US>0 else [])
    # In MEASURED mode primary-fill stall is structurally zero: optb_kvm_join
    # defines KVM = host_window - QEMU - FTL - NAND, so the four stages sum to
    # the matched window by construction and `first - body` vanishes.  What the
    # modelled path called primary-fill stall appears here as growth in the
    # KVM/QEMU/FTL segments instead.  Drop the empty band rather than plot it.
    _tail=[t for t in TAIL if not (MODE=="measured" and t=="primary-fill stall")]
    PSTAGES=PSERV+_tail
    def seg(name,s):
        dev=data[name][:,STAGES.index("FTL")]+data[name][:,STAGES.index("NAND")]
        if s=="FTL+NAND":
            # FEMU charges a fixed per-page transfer, so the split is exact
            return dev-np.minimum(CH_US,dev) if CH_US>0 else dev
        if s=="channel transfer":
            return np.minimum(CH_US,dev)
        return data[name][:,STAGES.index(s)]
    y=np.arange(len(PCTS))
    with plt.style.context(["science","ieee"]):
        plt.rcParams.update({"font.size":15,"axes.labelsize":16,"xtick.labelsize":14,
                             "ytick.labelsize":15,"legend.fontsize":13})
        fig,ax=plt.subplots(figsize=(8.0,4.8))
        # offsets negated so the FIRST config in `names` sits on TOP of each
        # group once the y-axis is inverted (3 us above 40 us).
        xmax=max(sum(seg(nm,s) for s in PSTAGES).max() for nm in names)
        for name,hatch,off in zip(names,hatches,[-0.21,+0.21]):
            base=np.zeros(len(PCTS))
            for s in PSTAGES:
                vals=seg(name,s)
                ax.barh(y+off,vals,height=0.38,left=base,color=color[s],
                        edgecolor="black",linewidth=0.4,hatch=hatch)
                base+=vals
            # total felt latency at the end of each bar (cf. felt_cmp_plot.py)
            for i in range(len(PCTS)):
                ax.text(base[i]+xmax*0.012,y[i]+off,f"{base[i]:.1f}",
                        va="center",ha="left",fontsize=11)
        ax.set_xlim(0,xmax*1.12)
        ax.set_yticks(y); ax.set_yticklabels([f"p{P:g}" for P in PCTS])
        # p50 at the TOP, matching felt_cmp_plot.py so the two breakdown
        # figures read the same way down the page.  The +/-0.21 offsets are
        # already in the same order as felt_cmp's +/-0.19, so inverting also
        # lines up which config sits above which within each group.
        ax.invert_yaxis()
        ax.grid(False); ax.margins(y=0.06)
        ax.set_xlabel(r"felt per-miss latency ($\mu$s)")
        H=fig.get_figheight(); fig.tight_layout(rect=(0,0,1,1-0.96/H))
        pat=lambda ns:[mpatches.Patch(facecolor=color[s],edgecolor="black",linewidth=0.5,label=s) for s in ns]
        # legend: servicing stages on row 1, tail components on row 2, configs on row 3
        fig.legend(handles=pat(PSERV),ncol=4,loc="upper center",bbox_to_anchor=(0.5,1-0.06/H))
        fig.legend(handles=pat(_tail),ncol=len(_tail),loc="upper center",bbox_to_anchor=(0.5,1-0.34/H))
        fig.legend(handles=[mpatches.Patch(facecolor="white",edgecolor="black",linewidth=0.5,
                                           hatch=h,label=LABEL.get(l,l)) for l,h in zip(names,hatches)],
                   ncol=2,loc="upper center",bbox_to_anchor=(0.5,1-0.62/H),columnspacing=1.6)
        _od=os.environ.get("OUTDIR","/mnt/nvme/cxdvirt/cxdvirt/reproduction/figures")
        out=f"{_od}/breakdown_refault{out_suf}.png"
        savefig(fig,out,dpi=300); plt.close(fig); print(f"wrote {out} (+.pdf)")

def build():
    if MODE=="measured":
        d,r=compute_measured(SUFS["100% miss"]); report("100% miss (measured)",d)
        for nm,v in r.items():
            print(f"  residual {nm}: "+"  ".join(f"p{P:g}={x:+.2f}" for P,x in zip(PCTS,v)))
        draw_cfgs(d,[c[0] for c in CFG],"_measured")
        return
    if MODE=="latcmp":
        d=compute(SUFS["100% miss"]); report("100% miss",d)
        draw_cfgs(d,["3us","40us"],"_lat2")
        return
    if MODE=="cmp":
        d100=compute(SUFS["100% miss"]); d50=compute(SUFS["50% miss"])
        report("100% miss",d100); report("50% miss",d50)
        draw([("100% miss",d100,None),("50% miss",d50,"///")],"_cmp")
    elif MODE=="50miss":
        d=compute(SUFS["50% miss"]); report("50% miss",d)
        draw([("50% miss",d,None)],"_50miss")
    else:
        d=compute(SUFS["100% miss"]); report("100% miss",d)
        draw([("100% miss",d,None)],"")

if __name__=="__main__": build()
