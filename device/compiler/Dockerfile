FROM ghcr.io/binozo/echogo:latest

# Minimum supported Android API level.
#
# biscuit (Echo Dot gen 2) runs FireOS 5 = Android 5.1 = API 22.
# checkers (Echo Show 5 gen 1) runs LineageOS 18.1 = Android 11 = API 30.
#
# The ABI is armeabi-v7a on both, despite checkers' 64-bit A53: its
# LineageOS build reports ro.product.cpu.abi=armeabi-v7a: so only the API
# level changes. Override at build time:
#
#   docker build --build-arg API=30 -t echomuse-compiler-api30 compiler/
ARG API=22
ENV API=${API}

ENV NDK_ROOT=/opt/android/ndk/21.4.7075529/toolchains/llvm/prebuilt/linux-x86_64
ENV CC=${NDK_ROOT}/bin/armv7a-linux-androideabi${API}-clang
ENV CXX=${NDK_ROOT}/bin/armv7a-linux-androideabi${API}-clang++
ENV AS=${NDK_ROOT}/bin/armv7a-linux-androideabi${API}-clang
ENV NDK_LIBS=${NDK_ROOT}/sysroot/usr/lib/arm-linux-androideabi/${API}
# --hash-style=both: the API 22 dynamic linker only understands SysV hashes,
# while newer lld emits GNU-style by default. Harmless at API 30.
ENV CGO_LDFLAGS="-Wl,--hash-style=both"

# Recompile tinyalsa against the selected API level
WORKDIR /tinyalsa/src
RUN make clean && \
    make CC=$CC LD=$LD AR=$AR AS=$AS libtinyalsa.a libtinyalsa.so && \
    cp /tinyalsa/src/libtinyalsa.so $NDK_LIBS/libtinyalsa.so

WORKDIR /sdk
