#!/bin/bash
# build.sh — builds crown_launcher.apk with no gradle, no Android Studio.
#
# Deliberately hand-rolled (aapt2 -> javac -> d8 -> apksigner) rather than a
# gradle project: this app is two small classes and a manifest with no
# resources, and a gradle/AGP toolchain would dwarf the thing it builds.
# Needs only cmdline-tools + one platform's build-tools + platform android.jar
# (see device/.android-sdk, installed 2026-08-26 for this exact purpose).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK="${ANDROID_HOME:-$HERE/../.android-sdk}"
BUILD_TOOLS="$SDK/build-tools/30.0.3"
PLATFORM_JAR="$SDK/platforms/android-30/android.jar"
OUT="$HERE/build"
PKG_PATH="com/echomuse/crownlauncher"

for f in "$BUILD_TOOLS/aapt2" "$BUILD_TOOLS/d8" "$BUILD_TOOLS/apksigner" "$PLATFORM_JAR"; do
    [ -e "$f" ] || { echo "✗ missing $f — is device/.android-sdk installed? (build-tools;30.0.3, platforms;android-30)" >&2; exit 1; }
done

rm -rf "$OUT"
mkdir -p "$OUT/gen" "$OUT/classes" "$OUT/dex"

echo "-- aapt2 link (manifest + resource table, no res/)"
"$BUILD_TOOLS/aapt2" link \
    -o "$OUT/app-unsigned.apk" \
    --manifest "$HERE/AndroidManifest.xml" \
    -I "$PLATFORM_JAR" \
    --java "$OUT/gen"

echo "-- javac"
javac -d "$OUT/classes" \
    -bootclasspath "$PLATFORM_JAR" \
    -source 8 -target 8 \
    -nowarn \
    "$HERE/src/$PKG_PATH"/*.java

echo "-- d8 (dex)"
"$BUILD_TOOLS/d8" --output "$OUT/dex" --lib "$PLATFORM_JAR" \
    "$OUT/classes/$PKG_PATH"/*.class

echo "-- pack classes.dex into apk"
cp "$OUT/dex/classes.dex" "$OUT/classes.dex"
( cd "$OUT" && zip -q app-unsigned.apk classes.dex )

echo "-- zipalign"
"$BUILD_TOOLS/zipalign" -f 4 "$OUT/app-unsigned.apk" "$OUT/app-aligned.apk"

echo "-- sign (debug key, generated on first run)"
# Standard Android debug-keystore convention (storepass/keypass "android"),
# deliberate for now: this APK is sideloaded to one dev unit, never
# distributed, so there's no update-integrity story a stable non-debug key
# would need to protect. Revisit if crown_launcher ever ships over OTA or to
# more than one device — that's a distribution model this key isn't for.
KEYSTORE="$HERE/debug.keystore"
if [ ! -f "$KEYSTORE" ]; then
    keytool -genkeypair -v -keystore "$KEYSTORE" -storepass android -keypass android \
        -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 \
        -dname "CN=EchoMuse Crown Launcher, OU=EchoMuse, O=EchoMuse, C=US" >/dev/null
fi
"$BUILD_TOOLS/apksigner" sign --ks "$KEYSTORE" --ks-pass pass:android \
    --out "$OUT/crown_launcher.apk" "$OUT/app-aligned.apk"

echo "== built: $OUT/crown_launcher.apk =="
echo "   install: adb install -r $OUT/crown_launcher.apk"
