// Tests for _md5Hex() in dashboard.jsx — the hash the emOS wizard flash step
// decides on.
//
//     node controller/tests/emos_md5.test.mjs
//
// Source extraction rather than import, for the reason boot_target.test.mjs
// gives: the dashboard compiles to a single classic script with no module
// boundary, so the alternative is a second copy that drifts.
//
// WHY THIS IS WORTH A TEST. md5 is here because the device verifies with
// busybox md5sum and crypto.subtle has no md5, so it is hand-written — and it
// is what decides whether a boot partition write is accepted. A wrong digest
// fails one way or the other and both are bad: a false mismatch refuses a
// perfectly good flash, and a false match accepts a corrupt one and reboots
// the device into it. Node's crypto is the oracle; nothing about the
// implementation is trusted.
//
// The length cases are not decoration. MD5 pads to a 64-byte block with a
// mandatory 0x80 and an 8-byte length, so 55, 56 and 64 bytes are the three
// boundaries where a padding mistake shows up and nowhere else does.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import { createHash, randomBytes } from "crypto";

const HERE = dirname(fileURLToPath(import.meta.url));
const JSX = join(HERE, "..", "static", "dashboard.jsx");
const src = readFileSync(JSX, "utf8");

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

const _md5Hex = new Function(`${liftFunction("_md5Hex")}; return _md5Hex;`)();

let failures = 0;
function check(name, cond, detail) {
  if (cond) return;
  failures++;
  console.error(`FAIL: ${name}${detail ? `\n      ${detail}` : ""}`);
}

const want = (buf) => createHash("md5").update(buf).digest("hex");

const cases = [
  ["the empty input", Buffer.alloc(0)],
  ["one byte", Buffer.from("a")],
  ["the RFC 1321 abc vector", Buffer.from("abc")],
  ["a short ascii string", Buffer.from("message digest")],
  ["the pangram", Buffer.from("The quick brown fox jumps over the lazy dog")],
  // The padding boundaries. 55 is the largest input that still fits its
  // length field in the same block; 56 forces a second block; 64 is an exact
  // block and needs a whole extra one.
  ["55 bytes, the last that fits one block", Buffer.alloc(55, 0x78)],
  ["56 bytes, the first that needs two", Buffer.alloc(56, 0x78)],
  ["exactly one block", Buffer.alloc(64, 0x78)],
  ["exactly two blocks", Buffer.alloc(128, 0x78)],
  // Bytes above 0x7f are where a signed/unsigned slip shows up, and a boot
  // image is nothing but bytes above 0x7f.
  ["high bytes", Buffer.alloc(200, 0xff)],
  ["a byte pattern spanning the range",
   Buffer.from(Array.from({ length: 512 }, (_, i) => i & 0xff))],
];

for (const [name, buf] of cases) {
  const got = await _md5Hex(new Uint8Array(buf));
  check(name, got === want(buf), `got ${got}, expected ${want(buf)}`);
}

// Random binary, which is the actual workload: the thing being hashed is a
// ~10MB boot image, not a string.
{
  let bad = 0;
  for (let t = 0; t < 100; t++) {
    const b = randomBytes(Math.floor(Math.random() * 4096));
    const got = await _md5Hex(new Uint8Array(b));
    if (got !== want(b)) bad++;
  }
  check("100 random buffers agree with node's crypto", bad === 0, `${bad} disagreed`);
}

// A boot-image-sized buffer, once. This is slow enough to be worth knowing
// about — the wizard hashes the image three times on the flash path — and it
// is the only case that exercises the loop at a realistic size.
{
  const big = randomBytes(4 * 1024 * 1024);
  const t0 = Date.now();
  const got = await _md5Hex(new Uint8Array(big));
  const ms = Date.now() - t0;
  check("a 4MB buffer hashes correctly", got === want(big), `got ${got}`);
  check("a 4MB buffer hashes in under 10s", ms < 10000, `took ${ms}ms`);
  console.log(`  (4MB in ${ms}ms)`);
}

// A single flipped bit must change the digest — the property the whole flash
// verification rests on.
{
  const a = Buffer.alloc(4096, 0x41);
  const b = Buffer.from(a); b[2000] ^= 0x01;
  const ha = await _md5Hex(new Uint8Array(a));
  const hb = await _md5Hex(new Uint8Array(b));
  check("one flipped bit changes the digest", ha !== hb, `${ha} === ${hb}`);
}

if (failures) {
  console.error(`\n${failures} check(s) failed.`);
  process.exit(1);
}
console.log("emos_md5: all checks passed.");
