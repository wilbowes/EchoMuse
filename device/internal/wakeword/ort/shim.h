/*
 * shim.h — the C half of the ONNX Runtime binding.
 *
 * Two reasons this file exists rather than calling ORT from Go directly:
 *
 *  1. ORT's C API is a struct of function pointers (OrtApi), and cgo cannot
 *     call a function pointer held in a C struct. Every call therefore needs a
 *     C-side wrapper, which is what most of this file is.
 *
 *  2. The library is dlopen'd at runtime, not linked. Nothing in the build
 *     references an ORT symbol, so the firmware links and boots on a device
 *     where libonnxruntime.so was never installed — em_ort_open simply fails
 *     and the caller falls back to controller-side wake word. Linking it
 *     properly would make a missing 12MB .so a boot failure, which on a device
 *     whose recovery story is "count fast exits and flip the A/B slot" is a
 *     bad trade.
 *
 * There is deliberately NO file-scope state here. An earlier version kept the
 * handle, the OrtApi and the OrtEnv in statics, which made a second Open with
 * a different path report a bookkeeping error ("already open") in place of the
 * real one — and made the missing-library path, the whole point of the dlopen
 * design, untestable once any library had loaded. Each em_runtime now owns its
 * handle, api and env, and every model carries the api pointer it was created
 * with.
 *
 * Error convention: every function returns char* — NULL on success, or a
 * malloc'd message the Go side turns into an error and frees. OrtStatus is
 * released here, at the point of failure, so no status can leak into Go.
 */
#ifndef EM_ORT_SHIM_H
#define EM_ORT_SHIM_H

#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "onnxruntime_c_api.h"

typedef const OrtApiBase *(*em_getapibase_fn)(void);

typedef struct {
	void *dl;
	const OrtApi *api;
	OrtEnv *env;
} em_runtime;

typedef struct {
	const OrtApi *api;
	OrtSession *sess;
	char *in_name;
	char *out_name;
	int xnnpack; /* whether the XNNPACK provider attached to this session */
} em_model;

static char *em_dup(const char *s) {
	size_t n = strlen(s) + 1;
	char *p = (char *)malloc(n);
	if (p) memcpy(p, s, n);
	return p;
}

/* em_err converts an OrtStatus into a malloc'd message and releases it. */
static char *em_err(const OrtApi *api, OrtStatus *st) {
	if (!st) return NULL;
	char *msg = em_dup(api->GetErrorMessage(st));
	api->ReleaseStatus(st);
	return msg ? msg : em_dup("onnxruntime: error (out of memory copying message)");
}

/* em_ort_open dlopens the runtime and creates an OrtEnv for it. */
static char *em_ort_open(const char *path, em_runtime *out) {
	memset(out, 0, sizeof(*out));

	/* RTLD_LOCAL: nothing else in the process should resolve against ORT's
	 * symbols, and libc++ is statically linked inside the .so, so exporting
	 * them globally risks clashing with the toolchain's own runtime. */
	void *dl = dlopen(path, RTLD_NOW | RTLD_LOCAL);
	if (!dl) {
		const char *e = dlerror();
		return em_dup(e ? e : "dlopen failed");
	}
	em_getapibase_fn base_fn = (em_getapibase_fn)dlsym(dl, "OrtGetApiBase");
	if (!base_fn) {
		const char *e = dlerror();
		char *msg = em_dup(e ? e : "OrtGetApiBase not found");
		dlclose(dl);
		return msg;
	}
	const OrtApiBase *base = base_fn();
	if (!base) {
		dlclose(dl);
		return em_dup("OrtGetApiBase returned NULL");
	}

	/* GetApi(ORT_API_VERSION) is the documented compatibility contract: a
	 * NEWER runtime honours an older requested version, an older one returns
	 * NULL. That is why a 1.19 header works against the 1.23 library used for
	 * host tests, and why a too-old library fails loudly here instead of
	 * subtly. */
	const OrtApi *api = base->GetApi(ORT_API_VERSION);
	if (!api) {
		char msg[192];
		snprintf(msg, sizeof(msg),
		         "onnxruntime is too old: it does not provide C API version %d (library reports %s)",
		         ORT_API_VERSION, base->GetVersionString());
		dlclose(dl);
		return em_dup(msg);
	}

	OrtEnv *env = NULL;
	OrtStatus *st = api->CreateEnv(ORT_LOGGING_LEVEL_ERROR, "echomuse", &env);
	if (st) {
		char *msg = em_err(api, st);
		dlclose(dl);
		return msg;
	}

	out->dl = dl;
	out->api = api;
	out->env = env;
	return NULL;
}

/* em_ort_version reports the loaded library's version, e.g. "1.19.2". */
static const char *em_ort_version(em_runtime *r) {
	if (!r->dl) return "";
	em_getapibase_fn base_fn = (em_getapibase_fn)dlsym(r->dl, "OrtGetApiBase");
	if (!base_fn) return "";
	return base_fn()->GetVersionString();
}

/*
 * em_model_load opens one model. The options are the measured optimum for a
 * duty-cycled wake word on this hardware, and two of them are counter-intuitive
 * enough to be worth stating here:
 *
 *   - allow_spinning=0. ORT's thread pool spin-waits for the next inference by
 *     default. In the ~60ms gap between 80ms frames that burns several times
 *     the CPU the inference itself needs (243% of a core measured on the
 *     device, against 36% with spinning off).
 *
 *   - threads=1. More threads halves latency and RAISES total CPU. We have
 *     51ms of headroom in an 80ms budget, so latency is free and CPU is not.
 *
 * XNNPACK is reachable only through the generic string-keyed EP API: the
 * Android AAR compiles the provider in but exports no
 * OrtSessionOptionsAppendExecutionProvider_Xnnpack symbol, so it looks
 * unavailable until asked for by name.
 */
static char *em_model_load(em_runtime *r, const char *path, int threads, int xnnpack,
                           int allow_spinning, em_model *out) {
	const OrtApi *api = r->api;
	if (!api) return em_dup("onnxruntime not open");
	memset(out, 0, sizeof(*out));
	out->api = api;

	OrtSessionOptions *o = NULL;
	char *err = em_err(api, api->CreateSessionOptions(&o));
	if (err) return err;

	if ((err = em_err(api, api->SetIntraOpNumThreads(o, threads))) ||
	    (err = em_err(api, api->SetSessionGraphOptimizationLevel(o, ORT_ENABLE_ALL)))) {
		api->ReleaseSessionOptions(o);
		return err;
	}
	if (!allow_spinning) {
		const char *off = "0";
		if ((err = em_err(api, api->AddSessionConfigEntry(o, "session.intra_op.allow_spinning", off))) ||
		    (err = em_err(api, api->AddSessionConfigEntry(o, "session.inter_op.allow_spinning", off)))) {
			api->ReleaseSessionOptions(o);
			return err;
		}
	}

	if (xnnpack) {
		char tb[16];
		snprintf(tb, sizeof(tb), "%d", threads);
		const char *keys[] = {"intra_op_num_threads"};
		const char *vals[] = {tb};
		OrtStatus *st = api->SessionOptionsAppendExecutionProvider(o, "XNNPACK", keys, vals, 1);
		if (st) {
			/* Deliberately non-fatal: fall back to the CPU provider, which
			 * produces bit-identical output for roughly 1.5x the CPU. */
			api->ReleaseStatus(st);
		} else {
			out->xnnpack = 1;
		}
	}

	OrtSession *sess = NULL;
	if ((err = em_err(api, api->CreateSession(r->env, path, o, &sess)))) {
		api->ReleaseSessionOptions(o);
		return err;
	}
	api->ReleaseSessionOptions(o);

	OrtAllocator *al = NULL;
	if ((err = em_err(api, api->GetAllocatorWithDefaultOptions(&al)))) {
		api->ReleaseSession(sess);
		return err;
	}
	char *in_name = NULL, *out_name = NULL;
	if ((err = em_err(api, api->SessionGetInputName(sess, 0, al, &in_name))) ||
	    (err = em_err(api, api->SessionGetOutputName(sess, 0, al, &out_name)))) {
		api->ReleaseSession(sess);
		return err;
	}
	out->sess = sess;
	out->in_name = in_name;
	out->out_name = out_name;
	return NULL;
}

/*
 * em_model_run executes one inference.
 *
 * `data` points at Go-owned memory and is only read for the duration of the
 * call (ORT wraps it without copying), which is what cgo's pointer rules
 * permit. The OUTPUT is copied into a fresh malloc'd buffer and the OrtValue
 * released before returning, so Go never holds a pointer into ORT's arena —
 * the alternative is handing back a pointer whose lifetime is tied to a C
 * object the garbage collector knows nothing about.
 */
static char *em_model_run(em_model *m, const float *data, size_t n_in,
                          const int64_t *shape, size_t ndim,
                          float **out_data, size_t *out_n) {
	const OrtApi *api = m->api;
	if (!api || !m->sess) return em_dup("model is closed");
	*out_data = NULL;
	*out_n = 0;

	OrtMemoryInfo *mi = NULL;
	char *err = em_err(api, api->CreateCpuMemoryInfo(OrtArenaAllocator, OrtMemTypeDefault, &mi));
	if (err) return err;

	OrtValue *in = NULL, *out = NULL;
	err = em_err(api, api->CreateTensorWithDataAsOrtValue(
	                      mi, (void *)data, n_in * sizeof(float), shape, ndim,
	                      ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &in));
	api->ReleaseMemoryInfo(mi);
	if (err) return err;

	err = em_err(api, api->Run(m->sess, NULL,
	                           (const char *const *)&m->in_name, (const OrtValue *const *)&in, 1,
	                           (const char *const *)&m->out_name, 1, &out));
	api->ReleaseValue(in);
	if (err) return err;

	OrtTensorTypeAndShapeInfo *ti = NULL;
	if ((err = em_err(api, api->GetTensorTypeAndShape(out, &ti)))) {
		api->ReleaseValue(out);
		return err;
	}
	size_t count = 0;
	err = em_err(api, api->GetTensorShapeElementCount(ti, &count));
	api->ReleaseTensorTypeAndShapeInfo(ti);
	if (err) {
		api->ReleaseValue(out);
		return err;
	}

	float *src = NULL;
	if ((err = em_err(api, api->GetTensorMutableData(out, (void **)&src)))) {
		api->ReleaseValue(out);
		return err;
	}
	float *copy = (float *)malloc(count * sizeof(float));
	if (!copy) {
		api->ReleaseValue(out);
		return em_dup("out of memory copying model output");
	}
	memcpy(copy, src, count * sizeof(float));
	api->ReleaseValue(out);

	*out_data = copy;
	*out_n = count;
	return NULL;
}

/*
 * em_vad_run runs one Silero VAD step (the v4 export openwakeword ships):
 * inputs `input` [1,n] float, `sr` int64 scalar, `h`/`c` [2,1,64] float;
 * outputs `output` [1,1], `hn`, `cn`. h and c are Go-owned, 128 floats each,
 * read as the carried state and overwritten with the next one, so a turn's
 * state lives in Go and one session serves any number of streams.
 */
#define EM_VAD_STATE 128
static char *em_vad_run(em_model *m, const float *x, size_t n,
                        float *h, float *c, float *prob) {
	const OrtApi *api = m->api;
	if (!api || !m->sess) return em_dup("model is closed");

	OrtMemoryInfo *mi = NULL;
	char *err = em_err(api, api->CreateCpuMemoryInfo(OrtArenaAllocator, OrtMemTypeDefault, &mi));
	if (err) return err;

	int64_t sr = 16000;
	const int64_t xs[] = {1, (int64_t)n};
	const int64_t ss[] = {2, 1, 64};
	OrtValue *in[4] = {NULL, NULL, NULL, NULL};
	err = em_err(api, api->CreateTensorWithDataAsOrtValue(mi, (void *)x, n * sizeof(float),
	                      xs, 2, ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &in[0]));
	if (!err) err = em_err(api, api->CreateTensorWithDataAsOrtValue(mi, &sr, sizeof(sr),
	                      NULL, 0, ONNX_TENSOR_ELEMENT_DATA_TYPE_INT64, &in[1]));
	if (!err) err = em_err(api, api->CreateTensorWithDataAsOrtValue(mi, h, EM_VAD_STATE * sizeof(float),
	                      ss, 3, ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &in[2]));
	if (!err) err = em_err(api, api->CreateTensorWithDataAsOrtValue(mi, c, EM_VAD_STATE * sizeof(float),
	                      ss, 3, ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &in[3]));
	api->ReleaseMemoryInfo(mi);

	static const char *in_names[] = {"input", "sr", "h", "c"};
	static const char *out_names[] = {"output", "hn", "cn"};
	OrtValue *out[3] = {NULL, NULL, NULL};
	if (!err) err = em_err(api, api->Run(m->sess, NULL, in_names, (const OrtValue *const *)in, 4,
	                                     out_names, 3, out));
	for (int i = 0; i < 4; i++)
		if (in[i]) api->ReleaseValue(in[i]);

	float *p = NULL, *hn = NULL, *cn = NULL;
	if (!err) err = em_err(api, api->GetTensorMutableData(out[0], (void **)&p));
	if (!err) err = em_err(api, api->GetTensorMutableData(out[1], (void **)&hn));
	if (!err) err = em_err(api, api->GetTensorMutableData(out[2], (void **)&cn));
	if (!err) {
		/* Inputs are released above, so overwriting the state they wrapped
		 * is safe. */
		*prob = p[0];
		memcpy(h, hn, EM_VAD_STATE * sizeof(float));
		memcpy(c, cn, EM_VAD_STATE * sizeof(float));
	}
	for (int i = 0; i < 3; i++)
		if (out[i]) api->ReleaseValue(out[i]);
	return err;
}

static void em_model_free(em_model *m) {
	const OrtApi *api = m->api;
	if (!api || !m->sess) return;
	api->ReleaseSession(m->sess);
	m->sess = NULL;

	/* in_name/out_name came from the default allocator. AllocatorFree is
	 * warn_unused_result: nothing useful can be done about a failure while
	 * tearing down, but the status still has to be released or freeing the
	 * names would itself leak. */
	OrtAllocator *al = NULL;
	OrtStatus *st = api->GetAllocatorWithDefaultOptions(&al);
	if (st) {
		api->ReleaseStatus(st);
		return;
	}
	if (m->in_name) {
		api->ReleaseStatus(api->AllocatorFree(al, m->in_name));
		m->in_name = NULL;
	}
	if (m->out_name) {
		api->ReleaseStatus(api->AllocatorFree(al, m->out_name));
		m->out_name = NULL;
	}
}

#endif /* EM_ORT_SHIM_H */
