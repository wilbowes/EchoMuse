// Tests for _bootImageLength() in dashboard.jsx — how much of a
// whole-partition escrow the wizard actually sends to the emOS builder.
//
//     node controller/tests/boot_image_length.test.mjs
//
// Source extraction rather than import, for the reason wifi_scan.test.mjs
// gives: the dashboard compiles to a single classic script with no module
// boundary, so the alternative is a second copy that drifts.
//
// This exists because sending the WHOLE 16MB partition put the build POST over
// Home Assistant's ingress body limit, and the 413 came back from the proxy
// without the request ever reaching the add-on — no controller log line, so
// nothing on the server side to read. The fix is arithmetic, and arithmetic
// that decides what reaches a boot-partition builder is worth pinning.
//
// Both fixtures are REAL headers, read off hardware 2026-09-06 with
// `dd if=/dev/block/mmcblk0p10 bs=2048 count=1 | hexdump`.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

function liftFunction(name) {
  const start = src.indexOf(`function ${name}`);
  if (start < 0) {
    throw new Error(`dashboard.jsx no longer defines ${name}() — if it was `
                  + `renamed or moved, update this test to match`);
  }
  let depth = 0;
  let i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) break; }
  }
  return src.slice(start, i + 1);
}

const { _bootImageLength } = await import(
  "data:text/javascript;base64," + Buffer.from(
    liftFunction("_bootImageLength") + "\nexport { _bootImageLength };"
  ).toString("base64"));

let failures = 0;
function check(what, ok, detail = "") {
  if (ok) { console.log(`ok    ${what}`); return; }
  failures++;
  console.error(`FAIL: ${what}${detail ? `\n      ${detail}` : ""}`);
}

const PARTITION = 16 * 1024 * 1024;   // p10 and p11 are both 32768 sectors

// Build a 16MB partition image carrying a boot header with these sizes.
function partition({ kernel, ramdisk, second = 0, page = 2048,
                     magic = "ANDROID!", size = PARTITION }) {
  const buf = new Uint8Array(size);
  buf.set(new TextEncoder().encode(magic), 0);
  const dv = new DataView(buf.buffer);
  dv.setUint32(8, kernel, true);
  dv.setUint32(16, ramdisk, true);
  dv.setUint32(24, second, true);
  dv.setUint32(36, page, true);
  return buf;
}

const upTo = (n, page) => Math.ceil(n / page) * page;

// G090LF1180440C95, slot A. The patched FireOS kernel this fleet runs.
{
  const k = 5792865, r = 2182034;
  const got = _bootImageLength(partition({ kernel: k, ramdisk: r }));
  const want = 2048 + upTo(k, 2048) + upTo(r, 2048);
  check("C95's real header resolves to its boot image", got === want,
        `got ${got}, expected ${want}`);
  check("and that is under half the partition", got < PARTITION / 2,
        `${got} of ${PARTITION}`);
}

// G090LF11803611NF, slot A — the device the 413 was measured on.
{
  const k = 0x586461, r = 0x214bcb;
  const got = _bootImageLength(partition({ kernel: k, ramdisk: r }));
  check("3611NF's real header resolves to its boot image",
        got === 2048 + upTo(k, 2048) + upTo(r, 2048), `got ${got}`);
  // The whole point: what we send has to clear HA's ingress limit with the
  // ~3.9MB init alongside it.
  check("reference plus init fits inside a 16MiB request body",
        got + 4039528 < 16 * 1024 * 1024,
        `${((got + 4039528) / 1024 / 1024).toFixed(1)} MB`);
}

// A second region that stock does not use, but the header can describe.
{
  const got = _bootImageLength(
    partition({ kernel: 4096, ramdisk: 4096, second: 4096 }));
  check("a second-stage region is counted", got === 2048 + 4096 * 3,
        `got ${got}`);
}

// Every refusal returns 0, meaning "send the whole partition". A size
// optimisation must never be the reason a build cannot happen.
{
  check("a non-boot image declines to trim",
        _bootImageLength(partition({ kernel: 4096, ramdisk: 4096, magic: "NOTABOOT" })) === 0);
  check("a zero page size declines to trim",
        _bootImageLength(partition({ kernel: 4096, ramdisk: 4096, page: 0 })) === 0);
  check("sizes that overrun the buffer decline to trim",
        _bootImageLength(partition({ kernel: PARTITION, ramdisk: PARTITION })) === 0);
  check("a truncated buffer declines to trim",
        _bootImageLength(new Uint8Array(512)) === 0);
  check("no buffer at all declines to trim", _bootImageLength(null) === 0);
}

// The trim must never cut into a region split_reference() reads: the header,
// the kernel (which it walks for the MTK wrapper and the DTB split) and the
// ramdisk all have to survive it.
{
  const k = 5792865, r = 2182034;
  const got = _bootImageLength(partition({ kernel: k, ramdisk: r }));
  const ramdiskEnd = 2048 + upTo(k, 2048) + r;
  check("the trim keeps the whole ramdisk", got >= ramdiskEnd,
        `trimmed at ${got}, ramdisk ends at ${ramdiskEnd}`);
}

if (failures) {
  console.error(`\n${failures} check(s) failed.`);
  process.exit(1);
}
console.log("boot_image_length: all checks passed.");
