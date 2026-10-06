# CXDVirt: Low Latency Kernel Module based CXL-SSD Emulation

CXDVirt is a CXL-SSD emulator implemented as a Linux kernel module.  The
emulated device is exposed through devdax; accesses to pages resident in the
emulated DRAM cache are served directly by the MMU, and misses are serviced in a
page-fault handler under a NAND performance model, with no virtual machine in
the path.

The repository has two parts:

- **`emulator/`** — the CXDVirt emulator: the kernel module, the host-kernel
  patch, the `daxmalloc` library, the `cxdvirt` command, documentation and an
  example.  It is self-contained and can be used independently of the rest of
  the repository.
- **`reproduction/`** — the paper's evaluation: experiment drivers, the
  instrumented Cylon used for comparison, benchmark patches and configurations,
  plotting code and the paper's measurements.  It uses the emulator only through
  `emulator/bin/` and `emulator/tools/daxmalloc/`.

## Documentation

| File | Contents |
|---|---|
| `emulator/README.md` | building the emulator, bringing up the device, requirements |
| `emulator/docs/PARAMETERS.md` | module parameters |
| `emulator/docs/OBSERVABILITY.md` | device counters |
| `emulator/docs/COMPATIBILITY.md` | mapping requirements and failure modes |
| `emulator/kernel/README.md` | host-kernel patch |
| `emulator/tools/daxmalloc/README.md` | heap placement on the device |
| `reproduction/scripts/README.md` | requirements, setup, and reproduction of each experiment and figure |
| `reproduction/MANIFEST.md` | each figure's commands, data files and configuration |
| `reproduction/results/README.md` | layout of the experiment output |
| `reproduction/bench-patches/README.md` | the Splash-4 timing patch |

## Repository layout

| Path | Contents |
|---|---|
| `emulator/bin/` | `cxdvirt` command, device bring-up and teardown, module source fetch |
| `emulator/nvmevirt-patch/` | the module, as a patch against NVMeVirt (`snu-csl/nvmevirt`, commit `a6a4252`) |
| `emulator/kernel/` | Linux 6.18.5 patch and configuration |
| `emulator/tools/daxmalloc/` | `LD_PRELOAD` library placing a program's heap on the device |
| `emulator/docs/`, `emulator/examples/` | documentation; a devdax latency example |
| `reproduction/scripts/` | fetch, build, experiment dispatch, figure drawing, device wrappers |
| `reproduction/drivers/` | per-experiment drivers: `cxdvirt/`, `cylon/`, `native/` |
| `reproduction/cylon/` | Cylon instrumentation patch against `MoatLab/Cylon` (`BASE_COMMIT`) and the host kernel configuration |
| `reproduction/tools/felt/` | `felt_probe` and `join_felt.py` (Fig. 6) |
| `reproduction/plots/tools/`, `reproduction/plots/data/` | plotting and analysis code; the paper's measurements |
| `reproduction/results/` | experiment output (empty as distributed) |
| `reproduction/bench/PINS.txt`, `reproduction/bench-patches/`, `reproduction/ycsb-configs/` | benchmark commits, patches and YCSB/Redis configuration |

`emulator/nvmevirt/`, `reproduction/cylon-tree/`, `reproduction/bench/<name>/`
and `reproduction/ndctl/` are downloaded by `reproduction/scripts/fetch.sh` and
are not included in the repository.
