/* pffft's external symbols under em_mel_ names. internal/aec includes the
   same vendored pffft.c (../../aec/src/pffft) under its own names, and both
   cgo objects link into one binary: without this, each symbol would be
   defined twice. pffft.c itself stays byte-identical. Every non-static
   function pffft.c defines is here; a new upstream one shows up as a
   duplicate-symbol link error, never silently. */
#define pffft_aligned_free em_mel_pffft_aligned_free
#define pffft_aligned_malloc em_mel_pffft_aligned_malloc
#define pffft_simd_size em_mel_pffft_simd_size
#define pffft_new_setup em_mel_pffft_new_setup
#define pffft_destroy_setup em_mel_pffft_destroy_setup
#define pffft_transform em_mel_pffft_transform
#define pffft_transform_ordered em_mel_pffft_transform_ordered
#define pffft_zreorder em_mel_pffft_zreorder
#define pffft_zconvolve_accumulate em_mel_pffft_zconvolve_accumulate
#define pffft_cplx_finalize em_mel_pffft_cplx_finalize
#define pffft_cplx_preprocess em_mel_pffft_cplx_preprocess
#define pffft_transform_internal em_mel_pffft_transform_internal
#define pffft_transform_internal_nosimd em_mel_pffft_transform_internal_nosimd
#define pffft_zreorder_nosimd em_mel_pffft_zreorder_nosimd
#define pffft_zconvolve_accumulate_nosimd em_mel_pffft_zconvolve_accumulate_nosimd
#define validate_pffft_simd em_mel_validate_pffft_simd
#define cffti1_ps em_mel_cffti1_ps
#define cfftf1_ps em_mel_cfftf1_ps
