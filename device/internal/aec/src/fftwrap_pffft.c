/* speex's FFT interface (fftwrap.h) on PFFFT, in place of fftwrap.c's
   kiss_fft. EchoMuse's, not speex's: the vendored speex files are unchanged.

   speex's packing, from kiss_fftr2: out[0] = DC, out[N-1] = Nyquist, bin k
   (1..N/2-1) at out[2k-1] (real) and out[2k] (imaginary). The forward
   transform is scaled by 1/N and the inverse is not, as fftwrap.c's float
   kiss path does. pffft's ordered real output holds DC and Nyquist in its
   first two floats, then bin k at 2k, 2k+1, unscaled both ways.

   pffft wants 16-byte aligned buffers for its SIMD path and speex's are
   not, so each table carries aligned in, out and work buffers; the copies
   are a small part of the transform. One table belongs to one echo state,
   which is used from one goroutine at a time (Canceller.mu). */

#include "pffft/pffft.h"
#include "fftwrap.h"

struct em_pffft {
   PFFFT_Setup *setup;
   float *in, *out, *work;
   int N;
};

void *spx_fft_init(int size)
{
   struct em_pffft *t = (struct em_pffft *)speex_alloc(sizeof(struct em_pffft));
   if (!t)
      return NULL;
   t->N = size;
   t->setup = pffft_new_setup(size, PFFFT_REAL);
   t->in = (float *)pffft_aligned_malloc(size * sizeof(float));
   t->out = (float *)pffft_aligned_malloc(size * sizeof(float));
   t->work = (float *)pffft_aligned_malloc(size * sizeof(float));
   if (!t->setup || !t->in || !t->out || !t->work) {
      speex_fatal("pffft: no setup for this frame size");
   }
   return t;
}

void spx_fft_destroy(void *table)
{
   struct em_pffft *t = (struct em_pffft *)table;
   pffft_destroy_setup(t->setup);
   pffft_aligned_free(t->in);
   pffft_aligned_free(t->out);
   pffft_aligned_free(t->work);
   speex_free(t);
}

void spx_fft(void *table, spx_word16_t *in, spx_word16_t *out)
{
   struct em_pffft *t = (struct em_pffft *)table;
   int N = t->N, k;
   float scale = 1.f / N;
   for (k = 0; k < N; k++)
      t->in[k] = in[k];
   pffft_transform_ordered(t->setup, t->in, t->out, t->work, PFFFT_FORWARD);
   out[0] = t->out[0] * scale;
   out[N - 1] = t->out[1] * scale;
   for (k = 1; k < N / 2; k++) {
      out[2 * k - 1] = t->out[2 * k] * scale;
      out[2 * k] = t->out[2 * k + 1] * scale;
   }
}

void spx_ifft(void *table, spx_word16_t *in, spx_word16_t *out)
{
   struct em_pffft *t = (struct em_pffft *)table;
   int N = t->N, k;
   t->in[0] = in[0];
   t->in[1] = in[N - 1];
   for (k = 1; k < N / 2; k++) {
      t->in[2 * k] = in[2 * k - 1];
      t->in[2 * k + 1] = in[2 * k];
   }
   pffft_transform_ordered(t->setup, t->in, t->out, t->work, PFFFT_BACKWARD);
   for (k = 0; k < N; k++)
      out[k] = t->out[k];
}

void spx_fft_float(void *table, float *in, float *out) { spx_fft(table, in, out); }
void spx_ifft_float(void *table, float *in, float *out) { spx_ifft(table, in, out); }

/* em_fft_parity runs one forward and one inverse transform of n samples of
   a fixed pseudo-random signal through this wrapper and through kiss_fftr
   with fftwrap.c's scaling, and returns the largest difference relative to
   the largest value: the test that the packing and scaling above are
   speex's. */
#include "kiss_fftr.h"
float em_fft_parity(int n)
{
   void *t = spx_fft_init(n);
   kiss_fftr_cfg kf = kiss_fftr_alloc(n, 0, NULL, NULL), ki = kiss_fftr_alloc(n, 1, NULL, NULL);
   float *x = (float *)speex_alloc(n * sizeof(float));
   float *a = (float *)speex_alloc(n * sizeof(float)), *b = (float *)speex_alloc(n * sizeof(float));
   float *c = (float *)speex_alloc(n * sizeof(float)), *d = (float *)speex_alloc(n * sizeof(float));
   unsigned s = 12345;
   float worst = 0, peak = 0;
   int k;
   for (k = 0; k < n; k++) {
      s = s * 1103515245u + 12345u;
      x[k] = (float)((int)(s >> 16) % 2000 - 1000);
   }
   spx_fft(t, x, a);
   kiss_fftr2(kf, x, b);
   for (k = 0; k < n; k++) {
      b[k] *= 1.f / n;
      if (fabsf(b[k]) > peak) peak = fabsf(b[k]);
   }
   for (k = 0; k < n; k++)
      if (fabsf(a[k] - b[k]) / peak > worst) worst = fabsf(a[k] - b[k]) / peak;
   spx_ifft(t, b, c);
   kiss_fftri2(ki, b, d);
   peak = 0;
   for (k = 0; k < n; k++)
      if (fabsf(d[k]) > peak) peak = fabsf(d[k]);
   for (k = 0; k < n; k++)
      if (fabsf(c[k] - d[k]) / peak > worst) worst = fabsf(c[k] - d[k]) / peak;
   spx_fft_destroy(t);
   kiss_fftr_free(kf);
   kiss_fftr_free(ki);
   speex_free(x); speex_free(a); speex_free(b); speex_free(c); speex_free(d);
   return worst;
}
