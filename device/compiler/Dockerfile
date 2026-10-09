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
# The digest below is the image that built every release up to v2.18.0-ea.1
# (Go 1.24.0, NDK 21.4.7075529). It was `:latest` when pinned, so this froze a
# known-good state rather than choosing a new one.
#
# To move it: pull the new digest deliberately, build, and put the binary on a
# real Dot before the pin changes. `go vet` and the host tests prove nothing
# here — they run on amd64 with the host toolchain, and this image is only ever
# exercised by compile.sh and release.yml.
FROM ghcr.io/binozo/echogo@sha256:c9456551c639290b1cc3e1df891177beadb5d0d7621a5c2ec81079a276019147

# The Go toolchain is replaced; the NDK, and everything else in the base, is
# not. The base carries Go 1.24.0, which left upstream support when 1.26 was
# released, and golang.org/x/crypto and x/sys now require 1.26 (#774).
# Upstream's :latest is still the digest above, so there is no newer image to
# move to. Swapping only /usr/local/go keeps this to ONE changed variable: the
# NDK 21 clang, the API 22 sysroot and the tinyalsa build below are untouched.
#
# The tarball is the official linux-amd64 release, checked against the sha256
# go.dev/dl publishes for it. GOTOOLCHAIN=local comes from the base and stays,
# so go.mod can never fetch a different compiler than this one.
#
# The rule above still holds: this pin moves only with a binary run on a Dot.
ENV GOLANG_VERSION=1.26.8
RUN curl -fsSL -o /tmp/go.tgz "https://go.dev/dl/go${GOLANG_VERSION}.linux-amd64.tar.gz" && \
    echo "d0f743b33e8d8945e6b1f432edd15785c70507121d6e2a723b21285eddf8b57b  /tmp/go.tgz" | sha256sum -c - && \
    rm -rf /usr/local/go && \
    tar -C /usr/local -xzf /tmp/go.tgz && \
    rm /tmp/go.tgz && \
    go version | grep -q "go${GOLANG_VERSION} "

ENV API=22
ENV CC=/opt/android/ndk/21.4.7075529/toolchains/llvm/prebuilt/linux-x86_64/bin/armv7a-linux-androideabi22-clang
ENV CXX=/opt/android/ndk/21.4.7075529/toolchains/llvm/prebuilt/linux-x86_64/bin/armv7a-linux-androideabi22-clang++
ENV AS=/opt/android/ndk/21.4.7075529/toolchains/llvm/prebuilt/linux-x86_64/bin/armv7a-linux-androideabi22-clang
ENV NDK_LIBS=/opt/android/ndk/21.4.7075529/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib/arm-linux-androideabi/22
ENV CGO_LDFLAGS="-Wl,--hash-style=both"

# Recompile tinyalsa for API 22
WORKDIR /tinyalsa/src
RUN make clean && \
    make CC=$CC LD=$LD AR=$AR AS=$AS libtinyalsa.a libtinyalsa.so && \
    cp /tinyalsa/src/libtinyalsa.so $NDK_LIBS/libtinyalsa.so

WORKDIR /sdk
