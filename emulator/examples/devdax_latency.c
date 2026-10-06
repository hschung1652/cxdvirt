/*
 * devdax_latency.c -- prove the emulator is doing what it claims, and show the
 * shape of the thing it emulates.
 *
 * A CXL-SSD's defining property is that its latency is BIMODAL: a hit in the
 * in-device DRAM cache is a load instruction, and a miss is a NAND read.  This
 * program measures both on the same mapping, by walking two working sets --
 * one comfortably inside the configured cache and one comfortably outside it --
 * and timing each 4 KiB touch with rdtscp.
 *
 * It is the smoke test for `cxdvirt selftest`.  If the resident walk does not
 * come back near DRAM speed, the mapping is not going through the module's
 * fault handler.  If the streaming walk does not come back near the compiled
 * tR, no NAND latency is being charged and every number the device produces is
 * meaningless.
 *
 * --prime IS REQUIRED FOR THE MISS PHASE TO MEAN ANYTHING.  On a freshly loaded
 * device the emulated NAND is empty, and a first touch of a page that was never
 * programmed has nothing to read: the module charges no read latency and
 * increments `nand_lat_skip_invalid` instead.  A cold read sweep therefore
 * measures the fault path alone -- about 1.5 us -- and looks like a suspiciously
 * fast NAND.  --prime writes the streaming region first, which programs those
 * pages through the eviction write-back path, so the timed reads afterwards are
 * real misses against valid NAND.  Always check `nand_lat_applied` in
 * /proc/nvmev/debug rather than trusting the timing alone.
 *
 * Build:  gcc -O2 -o devdax_latency devdax_latency.c
 * Run:    ./devdax_latency [--dev /dev/dax0.0] [--resident-mb N] [--stream-mb N]
 *
 * One mapping from offset 0 covers both phases.  The module also accepts
 * windows at non-zero offsets, up to 16 mappings; see docs/COMPATIBILITY.md.
 *
 * THE MAPPING LENGTH MUST BE A MULTIPLE OF THE DEVICE ALIGNMENT, which is
 * 2 MiB by default even though the module tracks 4 KiB pages.  devdax checks the
 * whole VMA, so an odd number of MiB is refused outright:
 *
 *   device_dax dax0.0: dax_mmap: fail, unaligned vma (... 0x1fffff)
 *   mmap: Invalid argument
 *
 * The alignment is read from sysfs rather than assumed, and the request is
 * rounded up to it.
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <x86intrin.h>

#define PAGE 4096UL

/* Device alignment from sysfs, e.g. /sys/bus/dax/devices/dax0.0/align.
 * Falls back to 2 MiB, which is the devdax default and what this module uses. */
static size_t dax_align(const char *dev)
{
	const char *name = strrchr(dev, '/');
	char path[256];
	unsigned long v = 0;
	FILE *f;

	name = name ? name + 1 : dev;
	snprintf(path, sizeof path, "/sys/bus/dax/devices/%s/align", name);
	f = fopen(path, "r");
	if (f) {
		if (fscanf(f, "%lu", &v) != 1) v = 0;
		fclose(f);
	}
	return v ? (size_t)v : (2UL << 20);
}

static int cmp_u64(const void *a, const void *b)
{
	uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
	return (x > y) - (x < y);
}

static double pct(uint64_t *v, size_t n, double p)
{
	if (!n) return 0.0;
	size_t i = (size_t)(p / 100.0 * (double)(n - 1) + 0.5);
	return (double)v[i];
}

/* TSC ticks per ns, measured against CLOCK_MONOTONIC.  The invariant TSC rate
 * is not the nominal clock and is not exposed portably, so measure it rather
 * than reading a model number. */
static double tsc_per_ns(void)
{
	struct timespec a, b;
	unsigned aux;
	clock_gettime(CLOCK_MONOTONIC, &a);
	uint64_t t0 = __rdtscp(&aux);
	struct timespec s = { 0, 50 * 1000 * 1000 };
	nanosleep(&s, NULL);
	uint64_t t1 = __rdtscp(&aux);
	clock_gettime(CLOCK_MONOTONIC, &b);
	double ns = (double)(b.tv_sec - a.tv_sec) * 1e9 + (double)(b.tv_nsec - a.tv_nsec);
	return (double)(t1 - t0) / ns;
}

/* One pass over `pages` 4 KiB pages starting at `base`, timing each touch.
 * `stride_pages` of 1 is sequential; anything larger defeats the module's
 * next-N prefetcher when that is enabled. */
static void walk(volatile char *base, size_t pages, size_t stride_pages,
		 uint64_t *out, int write)
{
	unsigned aux;
	for (size_t i = 0; i < pages; i++) {
		volatile char *p = base + (i * stride_pages) * PAGE;
		uint64_t t0 = __rdtscp(&aux);
		if (write) *p = (char)i; else (void)*p;
		uint64_t t1 = __rdtscp(&aux);
		out[i] = t1 - t0;
	}
}

static void report(const char *tag, uint64_t *v, size_t n, double tpn)
{
	qsort(v, n, sizeof *v, cmp_u64);
	double sum = 0;
	for (size_t i = 0; i < n; i++) sum += (double)v[i];
	printf("  %-22s n=%-8zu mean %8.3f us   p50 %8.3f   p99 %8.3f   p99.9 %8.3f\n",
	       tag, n, sum / (double)n / tpn / 1e3,
	       pct(v, n, 50) / tpn / 1e3, pct(v, n, 99) / tpn / 1e3,
	       pct(v, n, 99.9) / tpn / 1e3);
}

int main(int argc, char **argv)
{
	const char *dev = "/dev/dax0.0";
	size_t resident_mb = 64, stream_mb = 512, stride = 1;
	int do_write = 0, prime = 0;

	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--dev") && i + 1 < argc) dev = argv[++i];
		else if (!strcmp(argv[i], "--resident-mb") && i + 1 < argc) resident_mb = strtoul(argv[++i], 0, 0);
		else if (!strcmp(argv[i], "--stream-mb") && i + 1 < argc) stream_mb = strtoul(argv[++i], 0, 0);
		else if (!strcmp(argv[i], "--stride") && i + 1 < argc) stride = strtoul(argv[++i], 0, 0);
		else if (!strcmp(argv[i], "--write")) do_write = 1;
		else if (!strcmp(argv[i], "--prime")) prime = 1;
		else { fprintf(stderr, "usage: %s [--dev D] [--resident-mb N] [--stream-mb N]\n"
		                       "          [--stride N] [--write] [--prime]\n", argv[0]); return 2; }
	}

	int fd = open(dev, O_RDWR);
	if (fd < 0) { perror(dev); return 1; }

	size_t align = dax_align(dev);
	size_t want = (resident_mb + stream_mb) << 20;
	/* Round UP: mapping a little more than asked is harmless, and the phases
	 * only ever touch the first `want` bytes. */
	size_t need = (want + align - 1) & ~(align - 1);

	/* One mapping, offset 0, for the whole span both phases use. */
	void *m = mmap(NULL, need, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (m == MAP_FAILED) {
		perror("mmap");
		if (errno == EINVAL)
			fprintf(stderr,
				"  the device alignment is %zu bytes; the mapping was %zu\n"
				"  (dmesg will say \"unaligned vma\").  Pick sizes whose sum\n"
				"  is a multiple of %zu MiB.\n",
				align, need, align >> 20);
		return 1;
	}
	volatile char *base = m;

	double tpn = tsc_per_ns();
	size_t rp = (resident_mb << 20) / PAGE / stride;
	size_t sp = (stream_mb << 20) / PAGE / stride;
	uint64_t *v = malloc((rp > sp ? rp : sp) * sizeof *v);
	if (!v) { perror("malloc"); return 1; }

	printf("device %s   tsc %.4f GHz   stride %zu page(s)   %s\n",
	       dev, tpn, stride, do_write ? "write" : "read");
	printf("mapped %zu MiB (align %zu MiB)   resident %zu MiB   streaming %zu MiB\n",
	       need >> 20, align >> 20, resident_mb, stream_mb);

	/* Fault the resident set in first, then measure it warm.  The first pass
	 * is all misses by construction, so timing it would measure the fill path
	 * and not the hit path. */
	walk(base, rp, stride, v, 1);
	walk(base, rp, stride, v, do_write);
	report("resident (hit)", v, rp, tpn);

	/* Program the streaming region before timing reads over it.  Untimed: this
	 * pass is all write misses, and with the default cxl_wr_alloc=0 a write
	 * miss fetches nothing -- the page is simply marked dirty, and the NAND
	 * page becomes valid when the cache evicts it.  Because the region is
	 * larger than the cache, that eviction happens during this very pass. */
	if (prime) {
		walk(base + (resident_mb << 20), sp, stride, v, 1);
		printf("  primed %zu MiB (untimed writes; NAND now valid)\n", stream_mb);
	}

	/* A span past the resident set.  Whether these actually miss depends on
	 * dram_cache_mb: --stream-mb must exceed it for this to mean anything. */
	walk(base + (resident_mb << 20), sp, stride, v, do_write);
	report(prime ? "streaming (miss)" : "streaming (cold, unprimed)", v, sp, tpn);

	munmap(m, need);
	close(fd);
	return 0;
}
