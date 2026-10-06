# Host kernel for CXDVirt

Base: Linux 6.18.5 from kernel.org.  Two patches, generated against a pristine
tree and verified to reproduce the reference kernel source exactly.

    tar xf linux-6.18.5.tar.xz && cd linux-6.18.5
    patch -p1 < .../kernel/0001-cxdvirt-kernel-support.patch
    cp .../kernel/config-6.18.5-cxdvirt .config
    make olddefconfig && make -j"$(nproc)" && sudo make modules_install install

The kernel is booted with the command line given in the emulator README.
`0002` is optional.

## `0001-cxdvirt-kernel-support.patch` — required

Eight files.  Without any one of them, `nvmev.ko` either fails to load or
produces no device.

| file | what it does |
|---|---|
| `drivers/pci/quirks.c` | PCI fixups for the emulated endpoint (`0c51:0110`) and its root port (`0c51:0001`). The device's config space lives in reserved RAM reached over ECAM, and RAM cannot implement the BAR sizing protocol — writing `0xFFFFFFFF` to a BAR should read back a size mask, but RAM stores the value. The kernel therefore sizes BAR0 as 4 GB. These fixups rewrite the BAR resources to their real geometry (component registers 64 KB, device registers 64 KB, MSI-X table 4 KB), deriving the base from BAR0's start, which *is* read correctly. |
| `drivers/acpi/pci_root.c` | Adds `ACPI0016` (CXL host bridge) to `root_device_ids`, so the injected host bridge is probed as a PCI root bridge at all. |
| `drivers/acpi/acpi_configfs.c` | Accepts `CEDT` alongside `SSDT` when a table is loaded through configfs. The module injects a CXL Early Discovery Table; stock code rejects any signature but SSDT. |
| `drivers/acpi/bus.c` | Exports `acpi_bus_type`, so a module can register the ACPI devices behind that host bridge. |
| `drivers/dax/bus.c` | Un-statics and exports `dax_bus_type`, which is how `cxl_dax_hook.c` reaches the DAX device to substitute its `vm_operations_struct`. Without it there is no fault handler and therefore no emulation. |
| `drivers/cxl/port.c` | Adds `MODULE_ALIAS_CXL(CXL_DEVICE_ROOT)` so `cxl_port` will bind the emulated root port. |
| `drivers/cxl/core/port.c` | Makes `cxl_bus_match()` match the `cxl_port` driver against both `CXL_DEVICE_PORT` and `CXL_DEVICE_ROOT`. The pair with the line above: the alias offers the binding, this accepts it. |
| `mm/memory.c` | Exports `zap_page_range_single`, which is how the module tears a PTE down on eviction. Not reachable from a module otherwise. |

`vmf_insert_pfn_prot`, the other half of the fault path, needs **no** patch —
6.18.5 already exports it at `mm/memory.c:2678`.

## `0002-cxl-probe-logging-optional.patch` — optional

`dev_info`/`dev_err` tracing through `cxl_mem_probe`, `devm_cxl_enumerate_ports`
and `cxl_dport_alloc`.  No functional change; it reports where port enumeration
stops when a bring-up fails, and logs on every probe.
