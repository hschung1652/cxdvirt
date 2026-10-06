/*
 * conformance.c — assert that daxmalloc behaves exactly like the stock glibc
 * allocator, and that every byte it hands out really lives in the backing
 * region.
 *
 * Build without the shim; run under it:
 *   DAXMALLOC_PATH=/dev/shm/daxtest DAXMALLOC_SIZE=$((256<<20)) \
 *   DAXMALLOC_REQUIRE=1 LD_PRELOAD=./daxmalloc.so ./test/conformance
 *
 * The interesting assertions are not "does malloc return non-NULL" but:
 *   - results of glibc's OWN internal allocations (strdup, getline, realpath,
 *     scandir, getaddrinfo, ...) land in the region, i.e. glibc really does
 *     route its internals through the interposed symbols;
 *   - the program break and the anonymous mappings do NOT grow, i.e. nothing
 *     silently fell back to the normal heap;
 *   - the fiddly corners of the API (realloc(p,0), realloc(NULL,n),
 *     posix_memalign EINVAL rules, calloc zeroing over dirty memory) match
 *     glibc rather than dlmalloc defaults.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <glob.h>
#include <malloc.h>
#include <netdb.h>
#include <pthread.h>
#include <regex.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <wchar.h>
#include <dlfcn.h>

extern void *__libc_malloc(size_t);

static uintptr_t g_lo, g_hi;
static int g_pass, g_fail;

static void ok(int cond, const char *what)
{
	if (cond) {
		g_pass++;
	} else {
		g_fail++;
		printf("  FAIL: %s\n", what);
	}
}

/* A pointer counts as ours only if its whole usable extent is inside the
 * mapping -- catching a block that merely starts near the boundary. */
static int in_region(const void *p)
{
	uintptr_t a = (uintptr_t)p;
	size_t us;

	if (!p || !g_lo)
		return 0;
	if (a < g_lo || a >= g_hi)
		return 0;
	us = malloc_usable_size((void *)p);
	return a + us <= g_hi;
}

static void ok_in(const void *p, const char *what)
{
	if (!p) {
		g_fail++;
		printf("  FAIL: %s (NULL)\n", what);
		return;
	}
	if (!in_region(p)) {
		g_fail++;
		printf("  FAIL: %s -> %p is OUTSIDE the region [%#lx,%#lx)\n",
		       what, p, (unsigned long)g_lo, (unsigned long)g_hi);
		return;
	}
	g_pass++;
}

/* Locate the backing mapping by name, the same way an operator would when
 * checking a live benchmark process. */
static int find_region(const char *backing)
{
	char line[512];
	FILE *f = fopen("/proc/self/maps", "r");

	if (!f)
		return -1;
	while (fgets(line, sizeof(line), f)) {
		unsigned long lo, hi;

		if (!strstr(line, backing))
			continue;
		if (sscanf(line, "%lx-%lx", &lo, &hi) != 2)
			continue;
		if (hi - lo < (1u << 20))
			continue;
		if (!g_lo || lo < g_lo)
			g_lo = lo;
		if (hi > g_hi)
			g_hi = hi;
	}
	fclose(f);
	return g_lo ? 0 : -1;
}

static size_t count_big_anon(void)
{
	char line[512];
	size_t n = 0;
	FILE *f = fopen("/proc/self/maps", "r");

	if (!f)
		return 0;
	while (fgets(line, sizeof(line), f)) {
		unsigned long lo, hi;
		char *p;

		if (sscanf(line, "%lx-%lx", &lo, &hi) != 2)
			continue;
		if (hi - lo < (1u << 20))
			continue;
		p = strchr(line, '\n');
		if (p)
			*p = '\0';
		/* an anonymous mapping has nothing after the offset fields */
		if (strstr(line, "/") || strstr(line, "["))
			continue;
		n++;
	}
	fclose(f);
	return n;
}

/* ---------------------------------------------------------------- basics -- */
static void t_basic(void)
{
	static const size_t sizes[] = { 1, 7, 24, 100, 4096, 1u << 20 };
	size_t i;

	puts("[basic malloc/free]");
	for (i = 0; i < sizeof(sizes) / sizeof(sizes[0]); i++) {
		void *p = malloc(sizes[i]);

		ok_in(p, "malloc in region");
		ok(((uintptr_t)p & 15) == 0, "malloc 16-byte aligned");
		ok(malloc_usable_size(p) >= sizes[i], "usable_size >= request");
		memset(p, 0x5a, malloc_usable_size(p)); /* whole extent writable */
		free(p);
	}
	{
		void *z = malloc(0);

		ok(z != NULL, "malloc(0) returns non-NULL (glibc behaviour)");
		ok_in(z, "malloc(0) in region");
		free(z);
	}
	free(NULL); /* must not crash */
	ok(malloc_usable_size(NULL) == 0, "malloc_usable_size(NULL) == 0");
	g_pass++; /* free(NULL) survived */
}

/* ---------------------------------------------------------------- calloc -- */
static void t_calloc(void)
{
	size_t i, n = 1000 * 8;
	unsigned char *p;

	puts("[calloc]");
	/* The backing store is pre-filled with 0xAA, so this fails loudly if
	 * dlmalloc ever skipped the memset (it cannot with HAVE_MMAP=0, since
	 * there are no "known zero" mmap chunks -- this proves it). */
	p = calloc(1000, 8);
	ok_in(p, "calloc in region");
	for (i = 0; i < n; i++)
		if (p[i]) {
			printf("  FAIL: calloc byte %zu = 0x%02x, not zero "
			       "(dirty backing leaked through)\n", i, p[i]);
			g_fail++;
			break;
		}
	if (i == n)
		g_pass++;
	free(p);

	errno = 0;
	ok(calloc(SIZE_MAX / 2, 3) == NULL, "calloc overflow -> NULL");
	ok(errno == ENOMEM, "calloc overflow sets ENOMEM");
}

/* --------------------------------------------------------------- realloc -- */
static void t_realloc(void)
{
	char *p;
	size_t i;

	puts("[realloc]");

	/* realloc(NULL, n) must allocate from OUR heap.  A naive range check
	 * would route it to glibc, since NULL is in no region. */
	p = realloc(NULL, 100);
	ok_in(p, "realloc(NULL,100) allocates in region");

	memset(p, 'A', 100);
	p = realloc(p, 1u << 20);
	ok_in(p, "realloc grow 100 -> 1MiB in region");
	for (i = 0; i < 100; i++)
		if (p[i] != 'A')
			break;
	ok(i == 100, "realloc grow preserves contents");

	p = realloc(p, 10);
	ok_in(p, "realloc shrink 1MiB -> 10 in region");
	ok(p[0] == 'A' && p[9] == 'A', "realloc shrink preserves prefix");

	/* glibc frees and returns NULL; dlmalloc's default would shrink and
	 * return non-NULL. */
	ok(realloc(p, 0) == NULL, "realloc(p,0) returns NULL (glibc semantics)");

	p = reallocarray(NULL, 10, 10);
	ok_in(p, "reallocarray(NULL,10,10) in region");
	errno = 0;
	ok(reallocarray(p, SIZE_MAX, 2) == NULL, "reallocarray overflow -> NULL");
	ok(errno == ENOMEM, "reallocarray overflow sets ENOMEM");
	free(p);
}

/* ------------------------------------------------------------- alignment -- */
static void t_align(void)
{
	void *p = NULL;
	int rc;
	int saved;

	puts("[aligned allocation]");

	rc = posix_memalign(&p, 64, 100);
	ok(rc == 0, "posix_memalign(64,100) == 0");
	ok_in(p, "posix_memalign result in region");
	ok(((uintptr_t)p & 63) == 0, "posix_memalign 64-aligned");
	free(p);

	/* EINVAL cases: not a power of two, zero, and a power of two smaller
	 * than sizeof(void*).  None of them may touch errno or *memptr. */
	errno = 12345;
	p = (void *)0xdeadbeef;
	saved = posix_memalign(&p, 3, 100);
	ok(saved == EINVAL, "posix_memalign(3,..) == EINVAL");
	ok(errno == 12345, "posix_memalign does not set errno");
	ok(p == (void *)0xdeadbeef, "posix_memalign leaves *memptr on EINVAL");
	ok(posix_memalign(&p, 0, 100) == EINVAL, "posix_memalign(0,..) == EINVAL");
	ok(posix_memalign(&p, 4, 100) == EINVAL,
	   "posix_memalign(4,..) == EINVAL (< sizeof(void*))");

	p = aligned_alloc(64, 100);
	ok_in(p, "aligned_alloc(64,100) in region (size%align not enforced)");
	ok(((uintptr_t)p & 63) == 0, "aligned_alloc 64-aligned");
	free(p);

	errno = 0;
	ok(aligned_alloc(48, 96) == NULL, "aligned_alloc non-power-of-2 -> NULL");
	ok(errno == EINVAL, "aligned_alloc non-power-of-2 sets EINVAL");

	p = memalign(64, 10);
	ok_in(p, "memalign(64,10) in region");
	ok(((uintptr_t)p & 63) == 0, "memalign 64-aligned");
	free(p);

	p = valloc(100);
	ok_in(p, "valloc in region");
	ok(((uintptr_t)p % (size_t)sysconf(_SC_PAGESIZE)) == 0, "valloc page-aligned");
	free(p);

	p = pvalloc(100);
	ok_in(p, "pvalloc in region");
	ok(((uintptr_t)p % (size_t)sysconf(_SC_PAGESIZE)) == 0, "pvalloc page-aligned");
	ok(malloc_usable_size(p) >= (size_t)sysconf(_SC_PAGESIZE),
	   "pvalloc usable >= pagesize");
	free(p);
}

/* --------------------------------------------- glibc-internal allocations -- */
static void t_glibc_internals(void)
{
	char *s, *line = NULL;
	size_t cap = 0;
	FILE *f;
	struct dirent **names;
	int n, i;
	wchar_t *w;
	regex_t re;
	glob_t gl;
	struct addrinfo *ai = NULL, hints;
	void *h;

	puts("[glibc-internal allocations must land in the region]");

	s = strdup("hello daxmalloc");
	ok_in(s, "strdup");
	free(s);

	s = strndup("hello daxmalloc", 5);
	ok_in(s, "strndup");
	free(s);

	w = wcsdup(L"wide string");
	ok_in(w, "wcsdup");
	free(w);

	if (asprintf(&s, "%s-%d", "fmt", 42) >= 0) {
		ok_in(s, "asprintf");
		free(s);
	} else {
		ok(0, "asprintf succeeded");
	}

	/* getline() grows its buffer with realloc(NULL,...) then realloc(); it
	 * is the canonical exerciser of the realloc-NULL path. */
	f = fopen("/etc/passwd", "r");
	if (f) {
		ssize_t r = getline(&line, &cap, f);

		ok(r > 0, "getline read a line");
		ok_in(line, "getline buffer");
		while (getline(&line, &cap, f) > 0)
			;
		ok_in(line, "getline buffer after repeated growth");
		free(line);
		fclose(f);
	}

	s = realpath("/etc/../etc", NULL);
	ok_in(s, "realpath(path, NULL)");
	free(s);

	s = canonicalize_file_name("/etc");
	ok_in(s, "canonicalize_file_name");
	free(s);

	s = get_current_dir_name();
	ok_in(s, "get_current_dir_name");
	free(s);

	n = scandir("/etc", &names, NULL, alphasort);
	if (n >= 0) {
		ok_in(names, "scandir array");
		for (i = 0; i < n; i++) {
			if (i == 0)
				ok_in(names[i], "scandir entry");
			free(names[i]);
		}
		free(names);
	} else {
		ok(0, "scandir succeeded");
	}

	/* open_memstream's buffer is grown internally by realloc. */
	{
		char *buf = NULL;
		size_t len = 0;
		FILE *ms = open_memstream(&buf, &len);

		if (ms) {
			for (i = 0; i < 32768; i++)
				fputs("0123456789abcdef0123456789abcdef", ms);
			fclose(ms);
			ok(len >= (1u << 20), "open_memstream grew past 1MiB");
			ok_in(buf, "open_memstream buffer");
			free(buf);
		} else {
			ok(0, "open_memstream succeeded");
		}
	}

	if (regcomp(&re, "^[a-z]+$", REG_EXTENDED) == 0) {
		ok(regexec(&re, "abc", 0, NULL, 0) == 0, "regcomp/regexec work");
		regfree(&re);
	} else {
		ok(0, "regcomp succeeded");
	}

	memset(&gl, 0, sizeof(gl));
	if (glob("/etc/*", 0, NULL, &gl) == 0) {
		ok_in(gl.gl_pathv, "glob pathv");
		ok_in(gl.gl_pathv[0], "glob entry");
		globfree(&gl);
	} else {
		ok(0, "glob succeeded");
	}

	/* Drives NSS, which dlopen()s modules while the shim is live. */
	memset(&hints, 0, sizeof(hints));
	hints.ai_family = AF_UNSPEC;
	hints.ai_socktype = SOCK_STREAM;
	if (getaddrinfo("localhost", NULL, &hints, &ai) == 0 && ai) {
		ok_in(ai, "getaddrinfo result");
		freeaddrinfo(ai);
	} else {
		printf("  (skip: getaddrinfo unavailable)\n");
	}

	h = dlopen("libm.so.6", RTLD_NOW);
	if (h) {
		ok(dlsym(h, "sin") != NULL, "dlopen/dlsym work under the shim");
		dlclose(h);
	} else {
		ok(0, "dlopen succeeded");
	}
}

/* --------------------------------------------------- foreign-pointer path -- */
static void t_foreign(void)
{
	void *p, *q;

	puts("[foreign (glibc-allocated) pointers]");
	p = __libc_malloc(100);
	ok(p != NULL, "__libc_malloc returned memory");
	ok(!in_region(p), "__libc_malloc pointer is OUTSIDE our region");
	/* exercises the lazy dlsym path for foreign usable-size queries */
	ok(malloc_usable_size(p) >= 100, "usable_size works on a foreign pointer");

	q = realloc(p, 200);
	ok(q != NULL, "realloc of a foreign pointer succeeded");
	ok(!in_region(q), "realloc keeps a foreign block on the glibc heap");
	free(q); /* must route to __libc_free, not mspace_free */
	g_pass++;
}

/* ------------------------------------------------------ heap-growth proof -- */
static void t_no_leak_to_libc_heap(void)
{
	void *keep[64];
	void *brk0, *brk1;
	size_t anon0, anon1;
	int i;

	puts("[proof that the load did not land on the glibc heap]");
	brk0 = sbrk(0);
	anon0 = count_big_anon();

	for (i = 0; i < 64; i++) {
		keep[i] = malloc(1u << 20);
		ok_in(keep[i], "1MiB block in region");
		memset(keep[i], i, 1u << 20);
	}

	brk1 = sbrk(0);
	anon1 = count_big_anon();
	ok((size_t)((char *)brk1 - (char *)brk0) < (256u << 10),
	   "program break grew < 256KiB while 64MiB was allocated");
	ok(anon1 <= anon0, "no new >=1MiB anonymous mapping appeared");

	for (i = 0; i < 64; i++)
		free(keep[i]);
}

/* ---------------------------------------------------------------- threads -- */
#define NTHREAD 8
#define NITER 50000

static void *g_handoff[NTHREAD];
static pthread_barrier_t g_bar;
static int g_thread_bad;

static void *worker(void *arg)
{
	long id = (long)arg;
	unsigned seed = (unsigned)id * 2654435761u + 1;
	int i;

	for (i = 0; i < NITER; i++) {
		size_t n = 16 + (seed % 4096);
		void *p;

		seed = seed * 1103515245u + 12345u;
		p = malloc(n);
		if (!p || !in_region(p)) {
			g_thread_bad = 1;
			return NULL;
		}
		memset(p, (int)id, n);
		if (seed & 1) {
			p = realloc(p, n * 2);
			if (!p || !in_region(p)) {
				g_thread_bad = 1;
				return NULL;
			}
		}
		free(p);
	}

	/* Allocate here, free on another thread: the arena must be shared. */
	g_handoff[id] = malloc(4096);
	if (!g_handoff[id] || !in_region(g_handoff[id]))
		g_thread_bad = 1;
	pthread_barrier_wait(&g_bar);
	free(g_handoff[(id + 1) % NTHREAD]);
	return NULL;
}

static void t_threads(void)
{
	pthread_t th[NTHREAD];
	long i;
	void *big;

	puts("[threads]");
	pthread_barrier_init(&g_bar, NULL, NTHREAD);
	for (i = 0; i < NTHREAD; i++)
		pthread_create(&th[i], NULL, worker, (void *)i);
	for (i = 0; i < NTHREAD; i++)
		pthread_join(th[i], NULL);
	pthread_barrier_destroy(&g_bar);

	ok(!g_thread_bad, "all threaded allocations stayed in the region");
	big = malloc(16u << 20);
	ok_in(big, "large allocation still works after thread churn");
	free(big);
}

int main(void)
{
	const char *backing = getenv("DAXMALLOC_PATH");

	if (!backing || !*backing)
		backing = "/dev/dax0.0";

	if (find_region(backing) != 0) {
		fprintf(stderr,
			"conformance: no mapping of '%s' in /proc/self/maps -- "
			"the shim is not loaded, so there is nothing to test.\n"
			"Run with LD_PRELOAD=./daxmalloc.so and "
			"DAXMALLOC_REQUIRE=1.\n", backing);
		return 2;
	}
	printf("region: [%#lx, %#lx)  %.1f MiB   backing=%s\n\n",
	       (unsigned long)g_lo, (unsigned long)g_hi,
	       (double)(g_hi - g_lo) / (1024 * 1024), backing);

	t_basic();
	t_calloc();
	t_realloc();
	t_align();
	t_glibc_internals();
	t_foreign();
	t_no_leak_to_libc_heap();
	t_threads();

	printf("\n%d passed, %d failed\n", g_pass, g_fail);
	return g_fail ? 1 : 0;
}
