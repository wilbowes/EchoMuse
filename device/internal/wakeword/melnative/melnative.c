/* melnative.c — openWakeWord's melspectrogram model as a plain STFT on pffft.
 *
 * The ONNX graph is: Conv(512 taps, stride 160) with cos/sin kernels under a
 * 400-point periodic Hann window centred in 512 (56 zeros each side), power,
 * MatMul with a 257x32 mel table (4-15 taps per bin), clip at 1e-10, 10*log10,
 * then clip to the maximum over the whole call minus 80. This computes the
 * same thing with a 512-point real FFT per frame instead of two dense
 * 257x512 convolutions (2.1M MACs per 80ms chunk). */
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include "pffft.h"
#include "mel_table.h"

#define EM_NFFT 512
#define EM_WIN 400
#define EM_HOP 160
#define EM_PAD ((EM_NFFT - EM_WIN) / 2)

typedef struct {
	PFFFT_Setup *setup;
	float *in, *out, *work; /* pffft wants 16-byte aligned buffers */
	float win[EM_NFFT];
} em_mel;

static void em_mel_free(em_mel *m);

static em_mel *em_mel_new(void) {
	em_mel *m = (em_mel *)calloc(1, sizeof(*m));
	if (!m) return NULL;
	m->setup = pffft_new_setup(EM_NFFT, PFFFT_REAL);
	m->in = (float *)pffft_aligned_malloc(EM_NFFT * sizeof(float));
	m->out = (float *)pffft_aligned_malloc(EM_NFFT * sizeof(float));
	m->work = (float *)pffft_aligned_malloc(EM_NFFT * sizeof(float));
	if (!m->setup || !m->in || !m->out || !m->work) {
		em_mel_free(m);
		return NULL;
	}
	for (int n = 0; n < EM_NFFT; n++) {
		int k = n - EM_PAD;
		m->win[n] = (k >= 0 && k < EM_WIN) ? 0.5f - 0.5f * cosf(2.0f * (float)M_PI * (float)k / (float)EM_WIN) : 0.0f;
	}
	return m;
}

static void em_mel_free(em_mel *m) {
	if (!m) return;
	if (m->setup) pffft_destroy_setup(m->setup);
	pffft_aligned_free(m->in);
	pffft_aligned_free(m->out);
	pffft_aligned_free(m->work);
	free(m);
}

/* em_mel_frames is how many mel frames n samples yield. */
static int em_mel_frames(int n) { return n < EM_NFFT ? 0 : (n - EM_NFFT) / EM_HOP + 1; }

/* em_mel_run writes frames*32 values to out (row-major, frame then bin) and
 * returns the frame count. */
static int em_mel_run(em_mel *m, const float *x, int n, float *out) {
	int frames = em_mel_frames(n);
	float top = -INFINITY;
	for (int f = 0; f < frames; f++) {
		const float *src = x + f * EM_HOP;
		for (int i = 0; i < EM_NFFT; i++) m->in[i] = src[i] * m->win[i];
		pffft_transform_ordered(m->setup, m->in, m->out, m->work, PFFFT_FORWARD);
		/* ordered layout: out[0]=DC, out[1]=Nyquist, bin k at (2k, 2k+1) */
		float *row = out + f * EM_MEL_BINS;
		for (int b = 0; b < EM_MEL_BINS; b++) {
			float acc = 0.0f;
			int s = em_mel_start[b], nt = em_mel_ntaps[b];
			for (int t = 0; t < nt; t++) {
				int k = s + t;
				float re, im;
				if (k == 0) { re = m->out[0]; im = 0.0f; }
				else if (k == EM_NFFT / 2) { re = m->out[1]; im = 0.0f; }
				else { re = m->out[2 * k]; im = m->out[2 * k + 1]; }
				acc += (re * re + im * im) * em_mel_w[b][t];
			}
			if (acc < EM_MEL_CLIP_LO) acc = EM_MEL_CLIP_LO;
			/* log(x)*10/ln(10) as the graph does it, in float */
			float v = logf(acc) * 10.0f / 2.3025851f;
			row[b] = v;
			if (v > top) top = v;
		}
	}
	float lo = top - 80.0f;
	for (int i = 0; i < frames * EM_MEL_BINS; i++) {
		if (out[i] < lo) out[i] = lo;
	}
	return frames;
}
