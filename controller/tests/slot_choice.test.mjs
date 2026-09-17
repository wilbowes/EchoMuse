// Tests for classifyBootSlots() and chooseBootSlots() in dashboard.jsx — the
// rule that decides which boot slot keeps stock FireOS and which one emOS is
// written to.
//
//     node controller/tests/slot_choice.test.mjs
//
// Source extraction rather than import, for the reason boot_target.test.mjs
// gives: the dashboard compiles to one classic script with no module boundary.
//
// What is under test is the preservation of the only stock FireOS boot image
// on the device. It is the build reference for every future emOS image, and
// the only way back to FireOS — we ship neither a kernel nor a userspace, so
// once both slots hold emOS there is nothing on the device to rebuild from.
// The previous rule wrote whichever slot the device had BOOTED, which on a
// stock device is the slot the stock image is in, so it destroyed it every
// time.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

function liftFunction(name) {
  const start = src.indexOf(`function ${name}`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}()`);
  let depth = 0, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) break; }
  }
  return src.slice(start, i + 1);
}

const classifyBootSlots = eval(`(${liftFunction("classifyBootSlots")})`);
const chooseBootSlots = eval(`(${liftFunction("chooseBootSlots")})`);

let fails = 0;
function check(what, cond) {
  if (cond) { console.log(`ok    ${what}`); }
  else { console.log(`FAIL  ${what}`); fails++; }
}

const SYS = "SYS a /dev/block/mmcblk0p13\nSYS b /dev/block/mmcblk0p14";
const probe = (a, b) =>
  `SLOT a ${a} /dev/block/mmcblk0p10\nSLOT b ${b} /dev/block/mmcblk0p11\n${SYS}`;

// ── the parser ──────────────────────────────────────────────────────────────
{
  const s = classifyBootSlots(probe("stock", "empty"));
  check("parses both slot states", s.a.state === "stock" && s.b.state === "empty");
  check("parses slot devices", s.a.dev === "/dev/block/mmcblk0p10");
  check("parses the system map", s.sys.a === "/dev/block/mmcblk0p13");
}
{
  const s = classifyBootSlots("");
  check("nothing parsed reads as absent, never as usable",
        s.a.state === "absent" && s.b.state === "absent");
}

// ── where emOS goes: always slot A (#544) ────────────────────────────────────
//
// amonet v2's bootloader on biscuit starts boot_a whatever the BCB says; the
// BCB only changes androidboot.slot_suffix. Measured on the spare 2026-09-17
// (BCB B-active booted the image in boot_a with slot_suffix=_b) and in the
// #544 report (an image written to boot_b never ran). So the target is A in
// every case, and stock is kept in B.
const ALL = ["stock", "ours", "empty", "weird"];
{
  for (const a of ALL) for (const b of ALL) for (const suf of ["_a", "_b", ""]) {
    const r = chooseBootSlots(classifyBootSlots(probe(a, b)), suf);
    if (r.ok && r.target !== "a") {
      console.log(`FAIL  ${a}/${b}${suf}: target ${r.target}`); fails++;
    }
  }
  check("the target is slot A in every case, whatever the suffix says", true);
}
{
  // The #544 case: stock in both, booted A. Old rule wrote B, which never runs.
  const r = chooseBootSlots(classifyBootSlots(probe("stock", "stock")), "_a");
  check("stock/stock -> target A, built from A", r.ok && r.target === "a" && r.donor === "a");
  check("stock/stock -> B already stock, nothing to copy", r.ok && r.preserveDev === "");
  check("stock/stock -> system_a stamped", r.systemPart === 13);
  const r2 = chooseBootSlots(classifyBootSlots(probe("stock", "stock")), "_b");
  check("stock/stock with suffix _b decides the same way", r2.ok && r2.target === "a" && r2.donor === "a");
}
{
  // A holds the only stock image: copy it to B before overwriting A.
  for (const b of ["empty", "ours", "weird"]) {
    const r = chooseBootSlots(classifyBootSlots(probe("stock", b)), "_a");
    check(`stock/${b} -> copy A to B, then write A`,
          r.ok && r.target === "a" && r.donor === "a"
          && r.preserveDev === "/dev/block/mmcblk0p11");
  }
}
{
  // Stock only in B: the re-provision case. Build from B, leave B alone.
  for (const a of ["ours", "empty"]) {
    const r = chooseBootSlots(classifyBootSlots(probe(a, "stock")), "_a");
    check(`${a}/stock -> build from B, write A, copy nothing`,
          r.ok && r.donor === "b" && r.target === "a" && r.preserveDev === "");
    check(`${a}/stock -> system_b stamped`, r.systemPart === 14);
  }
}
{
  // The refusals.
  const r = chooseBootSlots(classifyBootSlots(probe("ours", "ours")), "_a");
  check("ours/ours refuses", !r.ok);
  check("ours/ours says how to recover", /escrowed boot image/.test(r.reason));
  check("empty/empty refuses", !chooseBootSlots(classifyBootSlots(probe("empty", "empty")), "_a").ok);
  check("no stock anywhere refuses", !chooseBootSlots(classifyBootSlots(probe("weird", "weird")), "_a").ok);
}

// ── things that must not become a write ─────────────────────────────────────
{
  // An unresolved system partition means an unstamped image, which silently
  // falls back to p13 — the WRONG userspace whenever the donor is B.
  const p = `SLOT a stock /dev/block/mmcblk0p10\nSLOT b empty /dev/block/mmcblk0p11`;
  check("no system map -> refuses rather than guessing", !chooseBootSlots(classifyBootSlots(p), "_a").ok);
}
{
  // A holds the only stock image and there is nowhere to keep a copy.
  const p = `SLOT a stock /dev/block/mmcblk0p10\nSLOT b absent\n${SYS}`;
  check("stock A with no slot B -> refuses", !chooseBootSlots(classifyBootSlots(p), "_a").ok);
  const p2 = `SLOT a stock /dev/block/mmcblk0p10\nSLOT b empty\n${SYS}`;
  check("stock A with an unresolved slot B -> refuses", !chooseBootSlots(classifyBootSlots(p2), "_a").ok);
}
{
  // Slot A unresolved: there is nowhere emOS can run from.
  const p = `SLOT a empty\nSLOT b stock /dev/block/mmcblk0p11\n${SYS}`;
  check("unresolved slot A -> refuses", !chooseBootSlots(classifyBootSlots(p), "_a").ok);
}
{
  // The stock copy is never the partition emOS is written to, and a stock
  // image in B is never overwritten.
  for (const a of ALL) for (const b of ALL) {
    const r = chooseBootSlots(classifyBootSlots(probe(a, b)), "_a");
    if (!r.ok) continue;
    if (r.preserveDev && r.preserveDev === r.targetDev) { console.log(`FAIL  copy===target ${a}/${b}`); fails++; }
    if (b === "stock" && r.preserveDev) { console.log(`FAIL  stock B overwritten ${a}/${b}`); fails++; }
    if (r.donor === "b" && r.donorDev !== "/dev/block/mmcblk0p11") { console.log(`FAIL  donorDev ${a}/${b}`); fails++; }
  }
  check("the copy never lands on the target, and a stock B is never overwritten", true);
}

// ── the flash and escrow use the plan ────────────────────────────────────────
{
  const flash = liftFunction("runFlashEmos");
  const copyAt = flash.indexOf("emosPlan.preserveDev");
  const writeAt = flash.indexOf("emosImage.bytes, emosImage.md5, 'emOS image'");
  check("the stock copy happens before emOS is written", copyAt > 0 && writeAt > copyAt);
  check("a failed copy stops before slot A is touched",
        /throw new Error\(`\$\{perr\}/.test(flash.slice(copyAt, writeAt)));
  check("the escrow reads the donor slot", /const refDev = plan \? plan\.donorDev : boot\.target;/.test(src));
}

// ── the probe's own marker test ─────────────────────────────────────────────
//
// classifyBootSlots only parses what the probe decided, so the decision of
// ours-vs-stock is made in SHELL and no test above can reach it. This checks
// the case statement itself, not the comment above it — a grep that matched
// the prose would be satisfied by the explanation of the bug.
//
// Both markers are required. `emos.system=` is stamped only since the stamp
// existed; every emOS image built before it carries `ramoops.mem_address=`
// and nothing else. Matching on the stamp alone read a FIELDED emOS image as
// stock — measured on the spare 2026-09-14, slot B — which would have made
// the wizard escrow an emOS image as the stock recovery image.
{
  const line = src.split("\n").find(l => l.includes("*emos.system=*") && l.includes("SLOT $x ours"));
  check("the probe classifies on the emos.system stamp", !!line);
  check("...and on ramoops, so pre-stamp emOS images are not read as stock",
        !!line && line.includes("ramoops.mem_address=0x44400000"));
  // The bare key would match a stock image that happened to carry ramoops at
  // some other address; reading stock as ours is the error that overwrites it.
  check("...by full address, not the bare ramoops key",
        !!line && !/ramoops\.mem_address=\*/.test(line));
}

console.log(fails ? "\nFAILED" : "\nall ok");
process.exit(fails ? 1 : 0);
