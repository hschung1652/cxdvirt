# CXDVirt — a kernel-module CXL-SSD emulator

CXDVirt emulates a CXL Type 3 memory device backed by NAND with a small
in-device DRAM cache. The device is exposed as `/dev/dax0.0`. Cache hits are
served by the MMU at native memory speed with no code in the path; a miss raises
a page fault that the module services under a NAND performance model. No virtual
machine or hypervisor is involved.

    ./bin/get_nvmevirt.sh              # upstream NVMeVirt + CXDVirt patch -> nvmevirt/
    ./bin/cxdvirt build
    sudo ./bin/cxdvirt up
    ./bin/cxdvirt selftest
    ./bin/cxdvirt run -- ./my_workload
    ./bin/cxdvirt stats
    sudo ./bin/cxdvirt down

`cxdvirt up` takes parameters as arguments. `bin/setup.sh` and `bin/teardown.sh`
take the same parameters as environment variables, which `sudo` strips, so they
need `env`:

    sudo env DRAM_CACHE_MB=8192 CACHE_POLICY=fifo ./bin/setup.sh

If `cxl` and `daxctl` are not on `PATH`, pass their paths the same way. Distro
packages older than ndctl v78 provide no `cxl` at all:

    sudo env CXL=/path/to/ndctl/build/cxl/cxl \
             DAXCTL=/path/to/ndctl/build/daxctl/daxctl ./bin/setup.sh

## Contents

| | |
|---|---|
| [`bin/cxdvirt`](bin/cxdvirt) | command interface: `build`, `up`, `down`, `status`, `stats`, `params`, `run`, `selftest` |
| `bin/setup.sh`, `bin/teardown.sh` | the bring-up and teardown sequences as plain scripts |
| `nvmevirt-patch/` | the module as a patch against upstream NVMeVirt, with base commit, remote and checksums |
| `bin/get_nvmevirt.sh` | clones upstream, applies the patch, verifies the result |
| `tools/daxmalloc/` | heap-on-devdax preload library |
| `kernel/` | two patches against Linux 6.18.5 and a working `.config` |
| `examples/` | a devdax workload measuring the bimodal latency profile |
| [`docs/PARAMETERS.md`](docs/PARAMETERS.md) | module parameter reference |
| [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) | mapping contract and failure modes |
| [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md) | device counter reference |

## Module source

The module ships as a patch against `snu-csl/nvmevirt` at `a6a4252`: 29 files,
comprising 20 new `cxl_*` and `cxlssd_*` files, a new ACPI table build script
(`scripts/compile_ssdt.sh`) and modifications to 8 upstream ones.

    git apply --stat nvmevirt-patch/cxdvirt-nvmevirt.patch

`bin/get_nvmevirt.sh` clones upstream, checks out the base commit, applies the
patch with `--binary` (two added files are compiled ACPI tables), and verifies
every resulting file against `SHA256SUMS`.

Network access is required once. `NVMEVIRT_SRC` may instead point at any clone
containing the base commit.

## Requirements

Linux 6.18.5 with the patches in [`kernel/`](kernel/README.md), and a physical
carve-out the module maps rather than allocates.

Boot parameters go in `/etc/default/grub`, in `GRUB_CMDLINE_LINUX`:

    GRUB_CMDLINE_LINUX="memmap=96G\\\$0x4600000000 isolcpus=20-24,60-64 nohz_full=20-24,60-64 rcu_nocbs=20-24,60-64 irqaffinity=0-19,40-59 intel_idle.max_cstate=1 processor.max_cstate=1 intremap=off nokaslr"

followed by `sudo update-grub` and a reboot.

**The `$` must be written `\\\$`.** It is unescaped twice before the kernel sees
it:

| stage | text |
|---|---|
| `/etc/default/grub` | `memmap=96G\\\$0x4600000000` |
| `/boot/grub/grub.cfg` | `memmap=96G\$0x4600000000` |
| `/proc/cmdline` | `memmap=96G$0x4600000000` |

Written bare, `$0x4600000000` expands as a shell variable and the reservation is
silently lost.  After reboot, `/proc/cmdline` contains the parameter:

    grep -o 'memmap=[^ ]*' /proc/cmdline        # memmap=96G$0x4600000000

`$` is not interchangeable with `!`: `$` marks the range reserved (E820 type 2),
`!` marks it persistent memory.

`intremap=off` and `nokaslr` are required for the emulated CXL host bridge to
initialize.

The isolation flags and C-state ceiling are not required for the device to
function, only for measurement. `isolcpus` must cover the cores named by `CPUS`
(default 20–23).

`ndctl` v78 or newer is required; earlier releases have no `cxl` subcommand.

## Verification

`cxdvirt selftest` confirms that latency is bimodal. It walks a working set
inside the DRAM cache and a second one that exceeds it, and checks three things:
the resident walk is served at host-DRAM speed, the streaming walk is charged at
least the compiled tR, and the `nand_lat_applied` counter advances. The third is
necessary because timing alone cannot distinguish a modelled NAND read from a
fast fault path.

## Using the device

**Direct mapping.** Any program that can `mmap` a devdax device can use it,
subject to the mapping contract in
[`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md).
[`examples/devdax_latency.c`](examples/devdax_latency.c) is a worked example.

**Heap redirection.** `cxdvirt run -- CMD` preloads `daxmalloc`, which serves the
malloc family from a single mapping of the device. Only the heap is relocated;
the binary, stack and libc remain in host DRAM.

## Operational notes

**Device contents persist across `rmmod`.** The backing store is physical memory
the module maps, so a second run finds its pages already programmed. Two runs
sharing a module load are not comparable. `ZERO_ON_INIT=true` avoids this, at
15–27 s per load over 96 GiB.

**Most parameters are load-time only** (mode `0444`), so sweeping them requires a
down/up cycle per point. `cxdvirt status` reports what the loaded module is
running with.

**`BG_DRAIN_4K`, `CLOCK_HW_YOUNG` and `ZERO_ON_INIT` change what a measurement
means.** Set differently across arms of a comparison, they invalidate it.

## Scope

4 KiB page granularity. The 2 MiB mode in the tree (`page_granularity=2m`,
`layer_size`, `nr_ranks`) is out of scope: `daxmalloc` refuses it and the
selftest assumes 4 KiB.

GPL-2.0, inherited from NVMeVirt. `tools/daxmalloc/malloc.c` is Doug Lea's
dlmalloc, public domain.
