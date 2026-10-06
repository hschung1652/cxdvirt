# Reproducing the Paper's Experiments

All commands are run from the `reproduction/` directory, and paths are relative
to it; the emulator is `../emulator/`.  `MANIFEST.md` maps each figure to its
targets and data files.

The workflow, in order:

1. Choose the machine-specific values (reservation address, cores).
2. Build and boot the CXDVirt host kernel.
3. Fetch and build, on the booted kernel.
4. Run the CXDVirt experiments.
5. Run the Cylon experiments (separate kernel and VM), or use the paper's Cylon
   data.
6. Draw the figures.

## Requirements

- **Hardware.**  An x86-64 machine with two NUMA nodes.  Node 0 runs the
  benchmarks; it needs at least 8 cores, and 96 GiB of free memory for the
  Cylon guest.  Node 1 holds the emulated device, a 96 GiB physical reservation,
  and provides the cores of the device's I/O workers.  The paper's platform:
  2× Intel Xeon Gold 5218R (20 cores per socket), 282 GiB DDR4 visible to the
  operating system.
- **Software.**  gcc (with OpenMP), make, meson, Python 3.8–3.11 (the versions
  for which the pinned packages of `plots/requirements.txt` install), a JRE and
  Python 2 (YCSB's launcher).  ndctl v78 or newer is fetched and built in step 3.
  Optionally LaTeX (on Ubuntu, `texlive-latex-extra`, `texlive-fonts-recommended`,
  `cm-super` and `dvipng`), with which Figs. 7 and 8 are typeset as published;
  without it they use matplotlib's own text rendering.

## 1. Machine-specific values

The scripts' defaults are the paper's machine.  On another machine, the values
below are chosen for its topology and passed as shown; node 0 for the
benchmarks and node 1 for the device are fixed.

| Value | Paper's machine | Requirement | Where it is set |
|---|---|---|---|
| Reservation start, `MEMMAP_START` | `0x4600000000` | a physical address in node 1 with 96 GiB of node-1 memory above it; `dmesg \| grep "Initmem setup node 1"` prints node 1's range | kernel command line (`memmap=`); `MEMMAP_START` |
| Reservation size | `96G` | unchanged; every experiment uses the 96 GiB device | kernel command line |
| Device I/O cores, `CPUS` | `20,21,22,23` | four cores on node 1 | `CPUS` |
| Fig. 6 probe core, `FELT_CPU` | `24` | a node-1 core not in `CPUS` | `FELT_CPU` |
| Isolated cores (CXDVirt kernel) | `20-24,60-64` | `CPUS` and `FELT_CPU` with their hyperthread siblings (`/sys/devices/system/cpu/cpuN/topology/thread_siblings_list`) | `isolcpus`, `nohz_full`, `rcu_nocbs` |
| Interrupt cores (CXDVirt kernel) | `0-19,40-59` | the node-0 cores | `irqaffinity` |
| Cylon vCPU cores, `VCPU_CPUS` | `4 5 6 7 8 9 10 11` | eight node-0 cores | `VCPU_CPUS` |
| Cylon FTL and poller cores, `FTL_CPUS`, `POLLER_CPUS` | `20 21`; `22 23 24 25 26 27 62 63` | node-1 cores | `FTL_CPUS`, `POLLER_CPUS` |
| Isolated cores (Cylon kernel) | `4-11,20-27,44-51,60-67` | the vCPU, FTL and poller cores with their siblings | `isolcpus`, `nohz_full`, `rcu_nocbs` |
| Interrupt cores (Cylon kernel) | `0-3,12-19,28-43,52-59,68-79` | the remaining cores | `irqaffinity` |

The variables are passed in the environment; `sudo` requires `env`:

    sudo env MEMMAP_START=<addr> CPUS=<a,b,c,d> FELT_CPU=<e> ./scripts/run_experiment.sh <target>
    MEMMAP_START=<addr> ./run-cxlssd.sh 98304                                   # Cylon launch
    sudo env VCPU_CPUS="<8 cores>" FTL_CPUS="<2 cores>" POLLER_CPUS="<8 cores>" drivers/cylon/pin_threads.sh

## 2. CXDVirt host kernel

Linux 6.18.5 is built with the emulator's kernel patch and configuration as
described in `../emulator/kernel/README.md`.  Its command line, in
`GRUB_CMDLINE_LINUX` of `/etc/default/grub`, with the values of step 1:

    memmap=96G\\\$0x4600000000 isolcpus=20-24,60-64 nohz_full=20-24,60-64 rcu_nocbs=20-24,60-64 irqaffinity=0-19,40-59 intel_idle.max_cstate=1 processor.max_cstate=1 intremap=off nokaslr

The `$` is written `\\\$` in `/etc/default/grub` (see `../emulator/README.md`).
After `sudo update-grub` and a reboot into 6.18.5,
`grep -o 'memmap=[^ ]*' /proc/cmdline` prints `memmap=96G$<MEMMAP_START>`.

## 3. Fetch and build

On the booted 6.18.5 kernel; `build.sh` compiles the emulator module against
the running kernel (`/lib/modules/$(uname -r)/build`).

    ./scripts/fetch.sh                 # NVMeVirt base for the emulator, Splash-4, Redis, ndctl, YCSB, MIO
    ./scripts/fetch.sh --cylon         # additionally, the full Cylon tree (step 5)
    ./scripts/build.sh all
    python3 -m venv plots/.venv && plots/.venv/bin/pip install -r plots/requirements.txt

The scripts contain absolute paths.  In a tree cloned or moved elsewhere,
`fetch.sh` and `make_figures.sh` rewrite them on their first run
(`scripts/relocate.sh`), and `run_experiment.sh` refuses to run until then.

## 4. CXDVirt experiments

`scripts/run_experiment.sh <target>` runs one experiment with the configuration
of the published figure and writes its results under `results/` (layout in
`results/README.md`).  `--dry-run` prints every command and module parameter
without running anything.  Except `fig7-native`, the targets require root and
an unloaded module; each loads and unloads the module for every point.

    ./scripts/run_experiment.sh fig6-felt-cxdvirt --dry-run
    sudo ./scripts/run_experiment.sh fig6-felt-cxdvirt

| Target | Figure | Time |
|---|---|---|
| `fig5-mio` | 5 | 90 min |
| `fig6-felt-cxdvirt` | 6 | 10 min |
| `fig7a-ocean` | 7(a) | 50 min |
| `fig7a-ocean-mode0` | 7(a), per-miss overhead in the text | 3 min |
| `fig7b-redis` | 7(b) | 20 min |
| `fig7-native` | 7, remote-DRAM lines (no device; no root) | 8 min |
| `fig8a-policy` | 8(a) | 1.6 h |
| `fig8bcd-prefetch` | 8(b)–(d) | 5.5 h |

`all-cxdvirt` runs the root targets above in sequence, all but `fig7-native`
(about 10 h).

## 5. Cylon experiments

Figs. 1, 5, 6 and 7 compare against Cylon.  For Figs. 5 and 7, as an
alternative to this step, `./scripts/seed_results.sh cylon` copies the paper's
Cylon series into `results/`, listing the copied files in
`results/SEEDED_FROM_REFERENCE.txt`; `make_figures.sh` reports their presence on
every run.  Figs. 1 and 6 require this step: their Cylon traces are not
included.

One-time setup:

1. Build and install Cylon's host kernel (from `fetch.sh --cylon`) with the
   supplied configuration:

       cp cylon/config-6.4.6-cylon cylon-tree/CylonLinux/.config
       make -C cylon-tree/CylonLinux olddefconfig
       make -C cylon-tree/CylonLinux -j"$(nproc)"
       sudo make -C cylon-tree/CylonLinux modules_install install

   Its command line differs from the CXDVirt kernel's: the reservation uses `!`,
   as Cylon's documentation specifies, and the isolated cores are Cylon's (step 1).
   `GRUB_CMDLINE_LINUX` is switched, followed by `sudo update-grub`, whenever the
   machine moves between the two kernels:

       memmap=96G!0x4600000000 isolcpus=4-11,20-27,44-51,60-67 nohz_full=4-11,20-27,44-51,60-67 rcu_nocbs=4-11,20-27,44-51,60-67 irqaffinity=0-3,12-19,28-43,52-59,68-79 intel_idle.max_cstate=1 processor.max_cstate=1

2. Build `cylon-tree/CylonFEMU` and a guest image (Ubuntu 22.04 with `ndctl`,
   `numactl`, `gcc` with OpenMP, `libnuma-dev` and a JRE) as described in
   `cylon-tree/README.md`.
3. With the VM running, from `reproduction/`: `drivers/cylon/guest_build_mio.sh`
   (MIO) and `drivers/cylon/guest_stage_redis.sh` (Redis and YCSB).  For OCEAN,
   the host's Splash-4 binary and the `daxmalloc` source are copied into the
   guest, and `daxmalloc` is built there:

       scp -P 8080 bench/Splash-4/Splash-4/ocean-contiguous_partitions/OCEAN-CONT root@localhost:/root/OCEAN
       scp -r -P 8080 ../emulator/tools/daxmalloc root@localhost:/root/daxmalloc-src
       ssh -p 8080 root@localhost 'make -C /root/daxmalloc-src daxmalloc.so && cp /root/daxmalloc-src/daxmalloc.so /root/'

Per VM boot, the VM is launched in one terminal, where QEMU stays in the
foreground with the guest console:

    cd cylon-tree/CylonFEMU/build-femu && <launch settings> ./run-cxlssd.sh 98304

Once the guest is up, the remaining steps run in a second terminal, from
`reproduction/`:

    sudo drivers/cylon/pin_threads.sh
    drivers/cylon/guest_setup_cxl.sh
    ./scripts/run_experiment.sh <target>

The guest is shut down (`poweroff`) before the next launch.

`guest_setup_cxl.sh` creates the CXL region in the guest, exposes it as
`/dev/dax0.0` (devdax) and warms it up by touching every page of the emulated
device, whose size it reads from the VM's launch parameters.  The Cylon drivers
refuse to run in a boot where this has not been done.

| Target | Figure | Launch settings | Boots | Time |
|---|---|---|---|---|
| `fig1-cylon-breakdown` (`PROFILE=3us`, `PROFILE=40us`) | 1; Cylon side of 6 | `CYLON_WB_OFF=1`; `PG_RD_LAT=40000` for the 40 µs profile | one per profile | 10, 20 min |
| `fig5-mio-cylon` | 5 | defaults | one | 35 min |
| `fig7a-ocean-cylon` | 7(a) | `BUFSZ_MB=<40578, 12912 or 6456> CYLON_FT_PROG=0 CYLON_EVICT_SYNC=3 CYLON_WM_HIGH=95 CYLON_WM_LOW=85 CYLON_WM_BATCH=1 CYLON_WB_SLOTS=4` | one per point | 15 min |
| `fig7b-redis-cylon` | 7(b) | `CYLON_WB_OFF=0 CYLON_EVICT_SYNC=3 CYLON_WM_HIGH=95 CYLON_WM_LOW=85 CYLON_WM_BATCH=1` | one | 30 min |

`run_experiment.sh <target> --dry-run` prints each target's launch settings.
`fig7b-redis-cylon` converts the guest's device to system RAM; that boot cannot
be used for other targets.

## 6. Drawing the figures

    ./scripts/make_figures.sh [fig1|fig5|fig6|fig7|fig8]     # from results/, into figures/
    ./scripts/make_figures.sh --reference                     # from plots/data/, into figures/reference/

Each figure is drawn from `results/`; a figure without results is skipped and
the targets that produce them are named.  Where a point has been run more than
once, the most recent run is used.  `fig7` also prints each number quoted in
the paper's Fig. 7 text, recomputed from the results, beside the published
value (`figures/fig7_numbers.txt`).  With `--reference`, the published Figs. 5,
7 and 8 are drawn from the paper's measurements in `plots/data/`, for
comparison.

| Figure | Targets |
|---|---|
| 1 | `fig1-cylon-breakdown` with `PROFILE=3us` and `PROFILE=40us` |
| 5 | `fig5-mio`, `fig5-mio-cylon` |
| 6 | `fig6-felt-cxdvirt`, and `fig1-cylon-breakdown` with `PROFILE=3us` |
| 7 | `fig7a-ocean`, `fig7a-ocean-mode0`, `fig7a-ocean-cylon`, `fig7b-redis`, `fig7b-redis-cylon`, `fig7-native` |
| 8 | `fig8a-policy`, `fig8bcd-prefetch` |

## Configuration

All runs use the 96 GiB device.  Except for Fig. 7(a), the DRAM cache is
4914 MB, matching Cylon's default buffer of 4915 MB, and the workload footprint
sets the normalized working-set size (WSS).

| Fig. | Workload | Footprint | WSS | CXDVirt eviction |
|---|---|---|---|---|
| 1 | MIO pointer chase (Cylon), 1 thread | 7400 MB | 1.5× | — |
| 5 | MIO pointer chase, 1 and 8 threads | 8390 MB; 8 × 1534 MB | 1.7×; 2.5× | slow drain, CLOCK (software bit) |
| 6 | `felt_probe` pointer chase, 1 thread | 7400 MB | 1.5× | slow drain, CLOCK (software bit) |
| 7(a) | Splash-4 OCEAN `-n8194`, 8 threads | 13.9 GiB | 0.35 / 1.1 / 2.2× (cache 40578 / 12912 / 6456 MB) | drain 4096 per 1 ms, CLOCK (software bit) |
| 7(b) | Redis YCSB-C, 1 thread | 1M / 3M / 6M records | 0.35 / 1.1 / 2.2× | fast drain, CLOCK (software bit) |
| 8(a) | Redis YCSB-C, FIFO / CLOCK / LIFO | 8M records | 2.65× | fast drain |
| 8(b)–(d) | Redis YCSB-C, CLOCK, next-N prefetch | 8M records | 2.65× | slow and fast drain |

Slow drain: 64 background evictions per 10 ms.  Fast drain: 1024 per 1 ms.
CLOCK (software bit): CLOCK without the hardware Accessed bit, matching the
behaviour of Cylon's CLOCK.

## Reference data

`plots/data/` (5 MB) holds the measurements from which the published Figs. 5, 7
and 8 are drawn, with each run's configuration record (`.meta`, `.setup`,
`meta.*`); `MANIFEST.md` lists the files.  The per-access traces of Figs. 1
and 6 (about 310 MB compressed) and the unprocessed captures are not included;
the targets of those figures regenerate them under `results/`.

Latency samples are stored as sorted int32 values (`.lat.gz`); the plotting
code reads the original files when present.

## Notes

- Cache size, policy, prefetch degree and drain settings are load-time module
  parameters.  Each point therefore reloads the module, and the live parameters
  are recorded with every result (`.meta`, `meta.*`).
- The carve-out retains its contents across module reloads.  OCEAN runs zero it
  at load (`ZERO_ON_INIT=true`).
- The remote-DRAM baselines run each workload as on CXDVirt, with `daxmalloc`
  placing only the heap on node 1, on Linux 6.18.5.
- Sample sizes: Cylon, one run per point; CXDVirt OCEAN, two; remote-DRAM
  baselines, three; all others, one.
- Figures report server-side Redis latency; client throughput is not comparable
  across boots.
- The reference Cylon Redis runs have no FEMU write-back counters;
  `drivers/cylon/run_redis_cylon.sh` records them.
