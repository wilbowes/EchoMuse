/* Hand-written replacement for the autoconf-generated header (the upstream
 * file is speexdsp_config_types.h.in). Fixed-width types via stdint — valid
 * for every platform this project targets (linux/arm, and linux/amd64 for
 * host-native unit tests in the compiler image). */
#ifndef __SPEEX_TYPES_H__
#define __SPEEX_TYPES_H__

#include <stdint.h>

typedef int16_t spx_int16_t;
typedef uint16_t spx_uint16_t;
typedef int32_t spx_int32_t;
typedef uint32_t spx_uint32_t;

#endif
