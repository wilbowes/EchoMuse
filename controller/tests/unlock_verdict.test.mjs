// Tests for _unlockVerdict() in dashboard.jsx — whether the wizard concludes
// an Echo was unlocked with amonet-biscuit v2.0.0 or later, and refuses it.
//
//     node controller/tests/unlock_verdict.test.mjs
//
// Source extraction rather than import, for the reason pm_verdict.test.mjs
// gives: the dashboard compiles to a single classic script with no module
// boundary, so the alternative is a second copy that drifts.
//
// Two ways to be wrong, and they are not equal. Missing a v2 device lets the
// wizard build and flash an image that cannot boot. Refusing a v1.1.0 device
// stops somebody whose Echo works. So the rule under test is that the verdict
// comes from EVIDENCE of v2, and that a probe which could not run — every
// input empty — is never evidence.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

function liftArrow(name) {
  const start = src.indexOf(`const ${name} = (`);
  if (start < 0) {
    throw new Error(`dashboard.jsx no longer defines ${name}() — if it was `
                  + `renamed or moved, update this test to match`);
  }
  let depth = 0;
  for (let i = start; i < src.length; i++) {
    const ch = src[i];
    if (ch === "{" || ch === "(" || ch === "[") depth++;
    else if (ch === "}" || ch === ")" || ch === "]") depth--;
    else if (ch === ";" && depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`could not find the end of ${name}`);
}

const { _unlockVerdict, _lkHead } = await import(
  "data:text/javascript;base64," + Buffer.from(
    liftArrow("_amonetLk") + "\n" + liftArrow("_lkHead") + "\n"
    + liftArrow("_unlockVerdict")
    + "\nexport { _unlockVerdict, _lkHead };"
  ).toString("base64"));

const LK_V1 = "8816885824790500";  // amonet 1's lk.bin: MTK magic, then the image length, 358,692
const LK_V2 = "88168858407b0500";  // amonet v2.0.0's lk.bin: MTK magic, then the image length, 359,232

let failures = 0;
function check(name, cond, detail) {
  if (cond) return;
  failures++;
  console.error(`FAIL: ${name}${detail ? `\n      ${detail}` : ""}`);
}
const v2 = (args) => _unlockVerdict(args).v2;

// ── A v1.1.0 device must pass, in every state the wizard can meet it in ──
check("v1 in TWRP: FireOS 5 on /system, v1's LK, TWRP 3.2.3",
  !v2({ release: "5.1.1", lk: LK_V1, twrp: "3.2.3-0" }));
check("v1 in Android: only the release is known",
  !v2({ release: "5.1.1" }));
check("every probe failed: nothing is evidence",
  !v2({ release: "", lk: "", twrp: "" }));
check("no arguments at all is not evidence either", !v2({}));
check("v1 in TWRP 3.7.0_9-bboe2, which firebreak installs on v1",
  !v2({ release: "5.1.1", lk: LK_V1, twrp: "3.7.0_9-bboe2" }));
check("an LK that is neither amonet's is not evidence",
  !v2({ release: "5.1.1", lk: "ffffffffffffffff", twrp: "3.2.3-0" }));
check("an MTK header of another length is not evidence",
  !v2({ release: "5.1.1", lk: "8816885800000300", twrp: "3.2.3-0" }));

// ── Each sign of v2 is enough on its own ──
check("amonet 2's LK in lk_a is v2",
  v2({ release: "5.1.1", lk: LK_V2, twrp: "3.2.3-0" }));
check("the header is matched whatever the hex case",
  v2({ lk: LK_V2.toUpperCase() }));
check("TWRP 3.7 is v2", v2({ twrp: "3.7.0_9-0" }));
check("TWRP 3.10 is compared as a number, not a string", v2({ twrp: "3.10.0" }));
check("TWRP 4.0 is v2", v2({ twrp: "4.0.0" }));
check("TWRP 3.6 is not v2", !v2({ twrp: "3.6.2_9-0" }));
check("a bboe TWRP does not hide v2's LK",
  v2({ lk: LK_V2, twrp: "3.7.0_9-bboe2" }));
check("FireOS 6 on /system (Android 7.1) is v2", v2({ release: "7.1.2" }));

// ── The refusal names what it found ──
const both = _unlockVerdict({ release: "7.1.2", lk: LK_V2, twrp: "3.7.0_9-0" });
check("all three signs are reported, so the message says why",
  both.evidence.length === 3, JSON.stringify(both.evidence));
check("the TWRP version appears in the evidence",
  both.evidence.some(e => e.includes("3.7.0_9-0")), JSON.stringify(both.evidence));

// ── The step-1 probe's decode: what the bytes pulled off lk_a become ──
// Step 1 copies eight bytes of lk_a to a file and decodes them here, so the
// test is over the bytes that really arrive, not over the hex in a string.
const lkBytes = (hex) => new Uint8Array(hex.match(/../g).map(h => parseInt(h, 16)));
const V1_BYTES = lkBytes(LK_V1), V2_BYTES = lkBytes(LK_V2);
check("the eight bytes of amonet 1's LK decode to its header",
  _lkHead(V1_BYTES) === LK_V1, _lkHead(V1_BYTES));
check("a longer read decodes only the header",
  _lkHead(new Uint8Array([...V1_BYTES, 0x0a, 0x0b])) === LK_V1);
check("amonet 1's header off lk_a is recognised, so v1 in TWRP is not refused",
  !v2({ release: "5.1.1", lk: _lkHead(V1_BYTES), twrp: "3.2.3-0" }));
check("v2's header read the same way is still refused",
  v2({ release: "5.1.1", lk: _lkHead(V2_BYTES), twrp: "3.2.3-0" }));
// The fallback the issue cares about: TWRP 3.2.3 with the probe unreadable is
// still a working amonet 1 device, so it must pass on the TWRP version.
check("no lk_a pulled at all: unreadable, not an LK", _lkHead(new Uint8Array(0)) === "");
check("a short read proves nothing", _lkHead(V1_BYTES.subarray(0, 4)) === "");
check("lk_a unreadable falls back on TWRP 3.2.3, which v1 installs",
  !v2({ release: "5.1.1", lk: _lkHead(new Uint8Array(0)), twrp: "3.2.3-0" }));
check("lk_a unreadable but TWRP 3.7 present is still evidence of v2",
  v2({ release: "5.1.1", lk: _lkHead(new Uint8Array(0)), twrp: "3.7.0_9-0" }));

// ── The connect step actually consults it, with the release from /system ──
// Comments stripped first: a guard that greps for a call also finds the
// comment explaining it, and passes on the prose.
const code = src.split("\n").filter(l => !l.trim().startsWith("//")).join("\n");
const connect = code.slice(code.indexOf("async function runConnectAndroid"));
check("runConnectAndroid calls _unlockVerdict",
  /_unlockVerdict\(\{\s*release:\s*effRelease/.test(connect));
check("in recovery the release comes from /system, not TWRP's getprop",
  /effRelease\s*=\s*\(sys && sys\.release\)/.test(connect));

// ── But the probe it reads must be able to RUN on amonet 1 ──
// It used to pipe lk_a through `od -An -tx1`, which TWRP 3.2.3's BusyBox
// 1.22.1 rejects ("invalid option -- A"), so on the recovery amonet 1
// installs the check never ran and the step fell back on the TWRP version
// (#903). Sliced to the one function: `od -An` still appears elsewhere.
const step1 = code.slice(code.indexOf("async function runConnectAndroid"),
                         code.indexOf("async function runConnectTwrp"));
check("the slice really is step 1", step1.includes("_UNLOCKCHK") && step1.length > 1000);
check("step 1 no longer decodes lk_a with od on the device", !/\bod\b/.test(step1));
check("step 1 copies lk_a's eight bytes to a file instead",
  /dd if="\$P" of=\/tmp\/em_lk\.bin bs=8 count=1/.test(step1));
check("step 1 decodes the pulled bytes in the browser",
  /_lkHead\(await c\.pull\('\/tmp\/em_lk\.bin'\)\)/.test(step1));

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("unlock_verdict: all checks passed");
