# Pinned by DIGEST, not by tag, and that is load-bearing rather than tidiness.
#
# This image carries the Go toolchain and the NDK that compile the firmware, so
# it is the layer sitting directly on top of FireOS 5 — Android 5.1, API 22,
# armv7a, a 2015 platform we cannot upgrade. `:latest` is a third party's
# floating tag: a rebuild upstream would change the compiler under us with no
# PR, no review and nothing in CI to notice, and the first symptom would be a
# binary that no longer runs on the hardware. Everything else in this project
# can be rolled back from the dashboard; a device that will not boot its new
# slot is recovered by hand.
#
# The digest below is the image that has built every shipped release to date
# (Go 1.24.0, NDK 21.4.7075529). It was `:latest` when pinned, so this froze a
# known-good state rather than choosing a new one.
#
# To move it: pull the new digest deliberately, build, and put the binary on a
# real Dot before the pin changes. `go vet` and the host tests prove nothing
# here — they run on amd64 with the host toolchain, and this image is only ever
# exercised by compile.sh and release.yml.
FROM ghcr.io/binozo/echogo@sha256:c9456551c639290b1cc3e1df891177beadb5d0d7621a5c2ec81079a276019147

ENV API=22
ENV CC=/opt/android/ndk/21.4.7075529/toolchains/llvm/prebuilt/linux-x86_64/bin/armv7a-linux-androideabi22-clang
ENV CXX=/opt/android/ndk/21.4.7075529/toolchains/llvm/prebuilt/linux-x86_64/bin/armv7a-linux-androideabi22-clang++
ENV AS=/opt/android/ndk/21.4.7075529/toolchains/llvm/prebuilt/linux-x86_64/bin/armv7a-linux-androideabi22-clang
ENV NDK_LIBS=/opt/android/ndk/21.4.7075529/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib/arm-linux-androideabi/22
ENV CGO_LDFLAGS="-Wl,--hash-style=both"

# Recompile tinyalsa for API 22
WORKDIR /tinyalsa/src
RUN make clean && \
    make CC="$CC" LD="$LD" AR="$AR" AS="$AS" libtinyalsa.a libtinyalsa.so && \
    cp /tinyalsa/src/libtinyalsa.so $NDK_LIBS/libtinyalsa.so

WORKDIR /sdk
