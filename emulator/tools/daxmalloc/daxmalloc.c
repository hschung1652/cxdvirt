/*
 * daxmalloc.c — LD_PRELOAD heap interposer that serves the whole malloc family
 * from a single mmap() of a devdax character device.
 *
 * Why this exists.  CXDVirt emulates a CXL-SSD as a devdax device only; it has
 * no system-ram/NUMA mode.  The macrobenchmarks it runs are unmodified
 * programs that reach memory through malloc()/new, and the conventional way
 * to place them on a memory device is `numactl --membind`.
 * This library replaces that placement mechanism: every heap byte comes out of
 * one MAP_SHARED mapping of /dev/dax0.0, so the benchmarks run unmodified on an
 * emulator that only speaks devdax.
 *
 * Module properties that shape the design (nvmevirt_fullcxl/):
 *   - The heap is one arena, so the device is mapped ONCE, at file offset 0, as
 *     a single VMA.  The module also supports windowed mappings at non-zero
 *     offsets (it adds vm_pgoff), but one mapping keeps a single entry in its
 *     16-slot mmap registry no matter how many allocations are made.
 *   - cxl_dax_hook.c's vm_operations_struct has .fault/.close but no .open, so
 *     a forked child's copied VMA is absent from the module's mmap registry and
 *     eviction PTE-shootdowns silently miss it.  Fork is therefore fatal to the
 *     experiment, not merely unsupported -- see fork_child_handler().
 *   - In 4K mode the device carries no metadata region, so the dlmalloc arena
 *     header at offset 0 is safe.  In 2M mode offset 0 is the shared chunk
 *     table; init refuses that geometry.
 *
 * Correctness rests on glibc's documented malloc-replacement contract: symbols
 * defined here preempt libc's at relocation time, before any constructor runs,
 * and glibc routes its own internal allocations (strdup, getline, asprintf,
 * realpath, NSS, dlopen...) through the preempted symbols.  Pointers we did not
 * hand out are routed back to __libc_* by an address-range check, so anything
 * unforeseen behaves exactly as it would with no shim loaded.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/sysmacros.h>
#include <sys/types.h>
#include <unistd.h>

/* ---- dlmalloc mspace API (malloc.c built with ONLY_MSPACES=1) ------------ */
typedef void *mspace;
extern mspace create_mspace_with_base(void *base, size_t capacity, int locked);
extern void *mspace_malloc(mspace, size_t);
extern void mspace_free(mspace, void *);
extern void *mspace_realloc(mspace, void *, size_t);
extern void *mspace_calloc(mspace, size_t, size_t);
extern void *mspace_memalign(mspace, size_t, size_t);
extern size_t mspace_usable_size(const void *);
extern size_t mspace_footprint(mspace);
extern size_t mspace_max_footprint(mspace);

/* ---- glibc internals, for pointers that are not ours --------------------- */
extern void *__libc_malloc(size_t);
extern void __libc_free(void *);
extern void *__libc_realloc(void *, size_t);
extern void *__libc_calloc(size_t, size_t);
extern void *__libc_memalign(size_t, size_t);

/* ---- state --------------------------------------------------------------- */
enum { ST_UNINIT = 0, ST_BUSY, ST_READY, ST_FALLBACK };

#define MAX_REGIONS 2 /* [0] = devdax; [1] reserved for DAXMALLOC_DRAM_BUDGET */

struct region {
	char *base;
	size_t size;
	mspace msp;
	const char *name;
	_Atomic size_t live, peak;   /* DAXMALLOC_STATS only */
};

static struct region g_reg[MAX_REGIONS];
static _Atomic int g_nreg;              /* release-stored last: gates find_region */
static _Atomic int g_state = ST_UNINIT;
static _Atomic long g_init_tid = -1;
static pthread_once_t g_once = PTHREAD_ONCE_INIT;
static _Atomic(size_t (*)(void *)) g_libc_usable;

static long g_pagesize = 4096;          /* x86-64; refreshed at init */
static int g_fd = -1;                   /* held open forever: holds the flock */
static int g_verbose, g_require, g_stats, g_fork_abort = 1;
/* g_reg[0] is always the device.  g_dram is non-NULL only when
 * DAXMALLOC_DRAM_BUDGET is set, in which case allocations try DRAM first and
 * spill to the device when it fills -- reproducing the first-touch spillover
 * an unpinned NUMA allocation produces. */
#define g_dev (&g_reg[0])
static struct region *g_dram;

/* ---- diagnostics: write(2) only. -----------------------------------------
 * stdio allocates its buffers through malloc, which would recurse straight
 * back into this file while init is still running.  Everything here is
 * syscall-only for that reason. */
static void wr(const char *s)
{
	size_t n = 0;
	while (s[n])
		n++;
	if (write(2, s, n) < 0)
		(void)0;
}

static void wrn(unsigned long v)
{
	char b[24];
	int i = sizeof(b);

	if (!v) {
		wr("0");
		return;
	}
	while (v) {
		b[--i] = (char)('0' + v % 10);
		v /= 10;
	}
	if (write(2, b + i, sizeof(b) - (size_t)i) < 0)
		(void)0;
}

/* Bounded string/number appenders so we never call snprintf (which can
 * allocate) while building sysfs paths during init. */
static char *app_s(char *p, char *end, const char *s)
{
	while (*s && p < end - 1)
		*p++ = *s++;
	*p = '\0';
	return p;
}

static char *app_u(char *p, char *end, unsigned long v)
{
	char b[24];
	int i = sizeof(b);

	if (!v)
		b[--i] = '0';
	while (v) {
		b[--i] = (char)('0' + v % 10);
		v /= 10;
	}
	while (i < (int)sizeof(b) && p < end - 1)
		*p++ = b[i++];
	*p = '\0';
	return p;
}

static int read_u64_file(const char *path, unsigned long long *out)
{
	char buf[64];
	ssize_t n;
	int fd, any = 0, i = 0;
	unsigned long long v = 0;

	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return -1;
	n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (n <= 0)
		return -1;
	buf[n] = '\0';
	while (buf[i] == ' ' || buf[i] == '\t')
		i++;
	for (; buf[i] >= '0' && buf[i] <= '9'; i++) {
		v = v * 10 + (unsigned)(buf[i] - '0');
		any = 1;
	}
	if (!any)
		return -1;
	*out = v;
	return 0;
}

/* dax attributes live under both /sys/dev/char/<maj>:<min>/ and
 * /sys/bus/dax/devices/<name>/ (the latter is what tools/felt/felt_probe.c
 * uses).  Try both so a renamed or symlinked device node still works. */
static int dax_attr(const struct stat *st, const char *path, const char *leaf,
		    unsigned long long *out)
{
	char p[192];
	char *q, *end = p + sizeof(p);
	const char *bn = path, *s;

	q = app_s(p, end, "/sys/dev/char/");
	q = app_u(q, end, major(st->st_rdev));
	q = app_s(q, end, ":");
	q = app_u(q, end, minor(st->st_rdev));
	q = app_s(q, end, "/");
	app_s(q, end, leaf);
	if (read_u64_file(p, out) == 0)
		return 0;

	for (s = path; *s; s++)
		if (*s == '/')
			bn = s + 1;
	q = app_s(p, end, "/sys/bus/dax/devices/");
	q = app_s(q, end, bn);
	q = app_s(q, end, "/");
	app_s(q, end, leaf);
	return read_u64_file(p, out);
}

/* CXDVirt's dax device is NOT all addressable heap.
 *
 * The physical carve-up is three regions (main.c:723, cxl_page_fault.c:1406):
 *   [memmap_start, +256MiB)              ECAM/registers -- outside the device
 *   [storage_start, +dram_cache_mb)      DRAM cache
 *   [storage_start+dram_cache, end)      NAND
 * and the dax device spans the last two (cxl_acpi_inject.c:55).  But the fault
 * handler treats a VMA offset as a NAND page index -- page_idx = (addr -
 * vm_start) >> PAGE_SHIFT, bounded by nr_pages = nand_size >> PAGE_SHIFT, then
 * installs nand_pfn_base + page_idx.  So the mapping is a window onto NAND
 * ONLY; in 4K mode the DRAM cache is a residency budget tracked by page-table
 * presence, never a separately addressable area.
 *
 * Mapping the device's full sysfs size therefore leaves a tail of exactly
 * dram_cache_mb worth of pages past nr_pages, which fault with VM_FAULT_SIGBUS.
 * dlmalloc carves upward from the bottom of the top chunk, so that tail is only
 * reached once the arena is nearly full: the process would run correctly for
 * hours and then take a SIGBUS instead of a clean ENOMEM.
 *
 * Returns the bytes to withhold, or 0 when this is not an nvmev device (a
 * plain devdax device, e.g. inside the Cylon guest, is addressable in full).
 */
/* Read a small text sysfs/procfs file.  Returns bytes read, or -1. */
static int read_str_file(const char *path, char *buf, size_t cap)
{
	ssize_t n;
	int fd = open(path, O_RDONLY | O_CLOEXEC);

	if (fd < 0)
		return -1;
	n = read(fd, buf, cap - 1);
	close(fd);
	if (n <= 0)
		return -1;
	buf[n] = '\0';
	return (int)n;
}

/* True when the module is in 2M page-granularity mode, where offset 0 of the
 * device holds the shared chunk table rather than data.
 *
 * Note this is NOT the same thing as the device's mmap alignment: a devdax
 * device reports align=2MiB regardless of the module's page granularity (it
 * constrains mapping length/offset only, which is why felt_probe.c uses it just
 * to round the length down).  Reading `align` to infer the mode gets 4K mode
 * wrong every time. */
static int cxdvirt_is_2m_mode(void)
{
	char buf[32];

	if (read_str_file("/sys/module/nvmev/parameters/page_granularity", buf,
			  sizeof(buf)) < 0)
		return 0; /* not an nvmev device */
	return buf[0] == '2';
}

static unsigned long long cxdvirt_dram_reserve(void)
{
	unsigned long long mb = 0;

	if (read_u64_file("/sys/module/nvmev/parameters/dram_cache_mb", &mb) != 0)
		return 0;
	return mb << 20;
}

/* Parse a byte count, accepting K/M/G suffixes ("16G", "16384M", raw bytes). */
static unsigned long long parse_size(const char *e)
{
	unsigned long long v = 0;
	int i = 0;

	if (!e || !*e)
		return 0;
	for (; e[i] >= '0' && e[i] <= '9'; i++)
		v = v * 10 + (unsigned)(e[i] - '0');
	switch (e[i]) {
	case 'g': case 'G': v <<= 30; break;
	case 'm': case 'M': v <<= 20; break;
	case 'k': case 'K': v <<= 10; break;
	default: break;
	}
	return v;
}

static void fail_init(const char *what)
{
	wr("daxmalloc: init failed: ");
	wr(what);
	wr(" (errno=");
	wrn((unsigned long)errno);
	wr(")\n");
	if (g_require) {
		wr("daxmalloc: DAXMALLOC_REQUIRE=1 -> aborting rather than "
		   "silently allocating from DRAM\n");
		abort();
	}
	atomic_store_explicit(&g_state, ST_FALLBACK, memory_order_release);
}

/* Runs in the child after fork().  The child's copy of the devdax VMA is not
 * in the module's registry, so evictions never shoot down its PTEs: it would
 * keep reading pages at DRAM speed with no NAND latency charged, and the run
 * would produce plausible but wrong numbers.  Aborting converts that silent
 * corruption into an obvious failure. */
static void fork_child_handler(void)
{
	wr("daxmalloc: fork() detected (child pid ");
	wrn((unsigned long)getpid());
	wr("): the child's devdax VMA is invisible to the module's eviction\n"
	   "daxmalloc: shootdown, so its timings would be silently wrong.\n");
	if (g_fork_abort) {
		wr("daxmalloc: aborting child (DAXMALLOC_FORK=warn overrides)\n");
		abort();
	}
}

static void stats_atexit(void)
{
	int i;

	if (!g_stats || atomic_load(&g_state) != ST_READY)
		return;
	for (i = 0; i < atomic_load(&g_nreg); i++) {
		wr("daxmalloc: ");
		wr(g_reg[i].name);
		wr(" peak_live=");
		wrn(atomic_load(&g_reg[i].peak));
		wr(" B (");
		wrn(atomic_load(&g_reg[i].peak) >> 20);
		wr(" MiB)  still_live=");
		wrn(atomic_load(&g_reg[i].live));
		wr(" B  capacity=");
		wrn(g_reg[i].size >> 20);
		wr(" MiB\n");
	}
}

/* Allocates nothing, directly or transitively: no stdio, no dlsym (it calls
 * calloc), no pthread_atfork until after the mspace is live (__register_atfork
 * allocates). */
static void do_init(void)
{
	const char *e, *path;
	struct stat st;
	unsigned long long size = 0, align = 4096, v;
	void *base;
	mspace msp;
	int fd;

	atomic_store(&g_init_tid, (long)syscall(SYS_gettid));
	atomic_store(&g_state, ST_BUSY);

	e = getenv("DAXMALLOC_VERBOSE");
	g_verbose = (e && *e == '1');
	e = getenv("DAXMALLOC_REQUIRE");
	g_require = (e && *e == '1');
	e = getenv("DAXMALLOC_STATS");
	g_stats = (e && *e == '1');
	e = getenv("DAXMALLOC_FORK");
	if (e && (*e == 'w' || *e == 'W'))
		g_fork_abort = 0;

	path = getenv("DAXMALLOC_PATH");
	if (!path || !*path)
		path = "/dev/dax0.0";

	fd = open(path, O_RDWR | O_CLOEXEC);
	if (fd < 0) {
		fail_init("open device");
		return;
	}
	if (fstat(fd, &st) != 0) {
		close(fd);
		fail_init("fstat");
		return;
	}

	e = getenv("DAXMALLOC_SIZE");
	if (e && *e) {
		unsigned long long t = 0;
		int i = 0, any = 0;

		for (; e[i] >= '0' && e[i] <= '9'; i++) {
			t = t * 10 + (unsigned)(e[i] - '0');
			any = 1;
		}
		if (any)
			size = t;
	}

	if (S_ISCHR(st.st_mode)) {
		if (!size && dax_attr(&st, path, "size", &v) == 0) {
			unsigned long long reserve = cxdvirt_dram_reserve();

			size = v;
			/* Only the NAND part of the device is addressable. */
			if (reserve && reserve < size) {
				size -= reserve;
				if (g_verbose) {
					wr("daxmalloc: withholding ");
					wrn((unsigned long)(reserve >> 20));
					wr(" MiB of DRAM-cache region; usable "
					   "NAND window ");
					wrn((unsigned long)(size >> 20));
					wr(" MiB\n");
				}
			}
		}
		if (dax_attr(&st, path, "align", &v) == 0 && v >= 4096)
			align = v;
		/* 2M mode puts the module's shared chunk table at offset 0,
		 * so a heap there would corrupt it. */
		if (cxdvirt_is_2m_mode()) {
			close(fd);
			fail_init("module is in 2M page_granularity mode "
				  "(offset 0 holds the shared chunk table); "
				  "load with page_granularity=4k");
			return;
		}
	} else if (S_ISREG(st.st_mode)) {
		/* Regular-file backing is the tmpfs test mode. */
		if (!size)
			size = (unsigned long long)st.st_size;
		if ((off_t)size > st.st_size && ftruncate(fd, (off_t)size) != 0) {
			close(fd);
			fail_init("ftruncate backing file");
			return;
		}
	} else {
		close(fd);
		fail_init("backing path is neither a char device nor a file");
		return;
	}

	if (align && (size % align))
		size = (size / align) * align; /* devdax mmap alignment */
	size &= ~(unsigned long long)4095;

	if (size < (1u << 20)) {
		close(fd);
		fail_init("region too small (need >= 1 MiB)");
		return;
	}

	/* One writer only.  The mapping is MAP_SHARED, so two processes each
	 * building an mspace at offset 0 would silently corrupt each other's
	 * arena -- and the module permits up to CXL_MAX_MMAP_ENTRIES mappings,
	 * so it will not stop us. */
	if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
		close(fd);
		fail_init("device busy (another daxmalloc process holds it)");
		return;
	}

	/* No MAP_POPULATE and no madvise: faulting IS the emulated access. */
	base = mmap(NULL, (size_t)size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (base == MAP_FAILED) {
		close(fd);
		fail_init("mmap");
		return;
	}

	msp = create_mspace_with_base(base, (size_t)size, 1 /* locked */);
	if (!msp) {
		munmap(base, (size_t)size);
		close(fd);
		fail_init("create_mspace_with_base");
		return;
	}

	g_fd = fd;
	g_pagesize = sysconf(_SC_PAGESIZE);
	if (g_pagesize <= 0)
		g_pagesize = 4096;
	g_reg[0].base = (char *)base;
	g_reg[0].size = (size_t)size;
	g_reg[0].msp = msp;
	g_reg[0].name = "device";
	atomic_store_explicit(&g_nreg, 1, memory_order_release);

	/* Optional DRAM budget.  A harness that applies no numactl fills the DRAM
	 * node by first touch and spills to CXL only once that is full.  Serving
	 * the first N bytes from ordinary anonymous
	 * memory reproduces that placement -- for a .sg load, allocation order and
	 * first-touch order coincide, so the approximation is close.  Without this
	 * the whole heap sits on the device, which is a stricter regime and not
	 * what their published numbers measure. */
	{
		unsigned long long budget = parse_size(getenv("DAXMALLOC_DRAM_BUDGET"));

		if (budget >= (1u << 20)) {
			void *db = mmap(NULL, (size_t)budget, PROT_READ | PROT_WRITE,
					MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE,
					-1, 0);
			mspace dmsp = (db == MAP_FAILED) ? NULL :
				create_mspace_with_base(db, (size_t)budget, 1);

			if (!dmsp) {
				wr("daxmalloc: DRAM budget mmap failed; "
				   "continuing device-only\n");
				if (db != MAP_FAILED)
					munmap(db, (size_t)budget);
			} else {
				g_reg[1].base = (char *)db;
				g_reg[1].size = (size_t)budget;
				g_reg[1].msp = dmsp;
				g_reg[1].name = "dram  ";
				atomic_store_explicit(&g_nreg, 2,
						      memory_order_release);
				g_dram = &g_reg[1];
				if (g_verbose) {
					wr("daxmalloc: DRAM budget ");
					wrn((unsigned long)(budget >> 20));
					wr(" MiB (allocations spill to the "
					   "device once it is full)\n");
				}
			}
		}
	}
	atomic_store_explicit(&g_state, ST_READY, memory_order_release);

	/* Both of these allocate; they must run with the mspace already live. */
	pthread_atfork(NULL, NULL, fork_child_handler);
	if (g_stats)
		atexit(stats_atexit);

	if (g_verbose) {
		wr("daxmalloc: mapped ");
		wr(path);
		wr(" size=");
		wrn((unsigned long)size);
		wr(" base=0x");
		{
			char b[17];
			int i = 16;
			unsigned long a = (unsigned long)base;

			b[16] = '\0';
			while (i--) {
				b[i] = "0123456789abcdef"[a & 0xf];
				a >>= 4;
			}
			wr(b);
		}
		wr("\n");
	}
}

static inline int ensure_init(void)
{
	int s = atomic_load_explicit(&g_state, memory_order_acquire);

	if (__builtin_expect(s == ST_READY || s == ST_FALLBACK, 1))
		return s;
	/* Re-entered from inside do_init on this same thread: serve from libc
	 * rather than deadlocking in pthread_once. */
	if (s == ST_BUSY &&
	    atomic_load(&g_init_tid) == (long)syscall(SYS_gettid))
		return ST_BUSY;
	pthread_once(&g_once, do_init);
	return atomic_load_explicit(&g_state, memory_order_acquire);
}

static inline struct region *find_region(const void *p)
{
	int n = atomic_load_explicit(&g_nreg, memory_order_acquire);
	int i;

	for (i = 0; i < n; i++) {
		const char *c = (const char *)p;

		if (c >= g_reg[i].base && c < g_reg[i].base + g_reg[i].size)
			return &g_reg[i];
	}
	return NULL;
}

/* Peak-usage accounting for DAXMALLOC_STATS.
 *
 * dlmalloc's mspace_footprint()/max_footprint() are useless here:
 * create_mspace_with_base() sets both to the full region size up front, so they
 * report capacity, not consumption.  Sizing a run needs the high-water mark of
 * live bytes, so track it -- but only when STATS is on, so measured runs pay
 * nothing beyond a predictable branch. */

static inline void reg_add(struct region *r, size_t n)
{
	size_t v, p;

	if (!g_stats)
		return;
	v = atomic_fetch_add(&r->live, n) + n;
	p = atomic_load(&r->peak);
	while (v > p && !atomic_compare_exchange_weak(&r->peak, &p, v))
		;
}

static inline void reg_sub(struct region *r, size_t n)
{
	if (g_stats)
		atomic_fetch_sub(&r->live, n);
}

/* Try DRAM (if budgeted) then the device.  dlmalloc returns NULL from a full
 * fixed mspace, which is exactly the spillover trigger we want. */
#define DAX_ALLOC_BODY(call, ...)                                       \
	do {                                                            \
		void *_p;                                               \
		if (g_dram) {                                           \
			_p = call(g_dram->msp, __VA_ARGS__);            \
			if (_p) {                                       \
				reg_add(g_dram, mspace_usable_size(_p));\
				return _p;                              \
			}                                               \
		}                                                       \
		_p = call(g_dev->msp, __VA_ARGS__);                     \
		if (_p)                                                 \
			reg_add(g_dev, mspace_usable_size(_p));         \
		else                                                    \
			errno = ENOMEM;                                 \
		return _p;                                              \
	} while (0)

static void *dax_alloc(size_t n)      { DAX_ALLOC_BODY(mspace_malloc, n); }
static void *dax_calloc(size_t n, size_t m) { DAX_ALLOC_BODY(mspace_calloc, n, m); }
static void *dax_memalign(size_t a, size_t n) { DAX_ALLOC_BODY(mspace_memalign, a, n); }

/* dlmalloc's mspace_malloc/calloc/realloc return 0 on failure WITHOUT running
 * MALLOC_FAILURE_ACTION -- only the non-mspace entry points set errno.  glibc
 * guarantees ENOMEM on every failed allocation, and callers rely on it (Redis's
 * OOM handler among them), so the guarantee is restored here rather than by
 * patching the vendored file. */


/* ---- interposed entry points --------------------------------------------- */

void *malloc(size_t n)
{
	if (__builtin_expect(ensure_init() == ST_READY, 1))
		return dax_alloc(n);
	return __libc_malloc(n);
}

void free(void *p)
{
	struct region *r;

	if (!p)
		return;
	r = find_region(p);
	if (r) {
		reg_sub(r, mspace_usable_size(p));
		mspace_free(r->msp, p);
	} else {
		__libc_free(p);
	}
}

void *calloc(size_t n, size_t m)
{
	if (__builtin_expect(ensure_init() == ST_READY, 1))
		return dax_calloc(n, m);
	return __libc_calloc(n, m);
}

void *realloc(void *p, size_t n)
{
	struct region *r;
	void *q;

	/* Must precede the range check: NULL is in no region, so plain
	 * range-routing would send realloc(NULL, n) -- the idiom behind
	 * getline() and open_memstream() -- to the glibc heap. */
	if (!p)
		return malloc(n);

	r = find_region(p);
	if (r) {
		size_t old;

		old = mspace_usable_size(p);
		if (n == 0) { /* glibc frees and returns NULL */
			reg_sub(r, old);
			mspace_free(r->msp, p);
			return NULL;
		}
		q = mspace_realloc(r->msp, p, n);
		if (q) {
			reg_sub(r, old);
			reg_add(r, mspace_usable_size(q));
			return q;
		}
		/* This region is full.  Growing a DRAM-budget block past the
		 * budget must spill to the device rather than fail, so retry
		 * through the normal allocator and move the data. */
		q = dax_alloc(n);
		if (!q) {
			errno = ENOMEM;
			return NULL;
		}
		memcpy(q, p, old < n ? old : n);
		reg_sub(r, old);
		mspace_free(r->msp, p);
		return q;
	}
	return __libc_realloc(p, n);
}

void *reallocarray(void *p, size_t nmemb, size_t size)
{
	size_t total;

	if (__builtin_mul_overflow(nmemb, size, &total)) {
		errno = ENOMEM;
		return NULL;
	}
	return realloc(p, total);
}

static void *memalign_core(size_t a, size_t n)
{
	if (__builtin_expect(ensure_init() == ST_READY, 1))
		return dax_memalign(a, n);
	return __libc_memalign(a, n);
}

int posix_memalign(void **memptr, size_t alignment, size_t size)
{
	int saved = errno;
	void *r;

	if (alignment < sizeof(void *) || (alignment & (alignment - 1)) != 0)
		return EINVAL;
	r = memalign_core(alignment, size);
	errno = saved; /* posix_memalign reports via return value only */
	if (!r)
		return ENOMEM;
	*memptr = r;
	return 0;
}

void *aligned_alloc(size_t alignment, size_t size)
{
	/* size % alignment is deliberately not enforced: glibc does not. */
	if (alignment == 0 || (alignment & (alignment - 1)) != 0) {
		errno = EINVAL;
		return NULL;
	}
	return memalign_core(alignment, size);
}

void *memalign(size_t alignment, size_t size)
{
	/* Rejected up front because dlmalloc would silently round a non-power
	 * of two up, where glibc reports EINVAL. */
	if (alignment == 0 || (alignment & (alignment - 1)) != 0) {
		errno = EINVAL;
		return NULL;
	}
	return memalign_core(alignment, size);
}

void *valloc(size_t size)
{
	ensure_init();
	return memalign_core((size_t)g_pagesize, size);
}

void *pvalloc(size_t size)
{
	size_t ps, rounded;

	ensure_init();
	ps = (size_t)g_pagesize;
	rounded = (size + ps - 1) & ~(ps - 1);
	if (rounded < size) { /* overflow */
		errno = ENOMEM;
		return NULL;
	}
	if (rounded == 0)
		rounded = ps;
	return memalign_core(ps, rounded);
}

size_t malloc_usable_size(void *p)
{
	size_t (*fn)(void *);
	struct region *r;

	if (!p)
		return 0;
	r = find_region(p);
	if (r)
		return mspace_usable_size(p);

	/* Foreign pointer.  dlsym() can call calloc(), so it is resolved here
	 * -- lazily, after the mspace exists -- and never during init. */
	fn = atomic_load(&g_libc_usable);
	if (!fn) {
		int s = atomic_load(&g_state);

		if (s != ST_READY && s != ST_FALLBACK)
			return 0;
		fn = (size_t (*)(void *))dlsym(RTLD_NEXT, "malloc_usable_size");
		atomic_store(&g_libc_usable, fn);
	}
	return fn ? fn(p) : 0;
}

/* Eager init so a misconfigured run fails before main() rather than after an
 * hour of benchmarking, and so the mspace exists before any thread is spawned.
 * The lazy path above still covers allocations made by other preloaded
 * libraries' constructors, which can run before this one. */
__attribute__((constructor)) static void daxmalloc_ctor(void)
{
	ensure_init();
}
