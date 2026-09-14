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

// ── the four cases ──────────────────────────────────────────────────────────
{
  // Stock in A, emOS already in B: the re-provision case. Must rebuild from
  // stock and overwrite ours, so running the wizard twice is idempotent and
  // never eats the stock image.
  const r = chooseBootSlots(classifyBootSlots(probe("stock", "ours")), "_a");
  check("stock/ours -> donor A, target B", r.ok && r.donor === "a" && r.target === "b");
  check("stock/ours -> system_a stamped", r.systemPart === 13);
}
{
  // Stock in B this time. The donor is chosen by WHERE STOCK IS, not by which
  // slot booted — and the stamp follows the donor, so the image is built
  // against the userspace its kernel came from.
  const r = chooseBootSlots(classifyBootSlots(probe("ours", "stock")), "_a");
  check("ours/stock -> donor B, target A", r.ok && r.donor === "b" && r.target === "a");
  check("ours/stock -> system_b stamped", r.systemPart === 14);
}
{
  const r = chooseBootSlots(classifyBootSlots(probe("stock", "empty")), "_a");
  check("stock/empty -> donor A, target B", r.ok && r.donor === "a" && r.target === "b");
}
{
  // Both stock: keep the one the device actually boots, take the other.
  const r = chooseBootSlots(classifyBootSlots(probe("stock", "stock")), "_a");
  check("stock/stock booted A -> donor A", r.ok && r.donor === "a" && r.target === "b");
  const r2 = chooseBootSlots(classifyBootSlots(probe("stock", "stock")), "_b");
  check("stock/stock booted B -> donor B", r2.ok && r2.donor === "b" && r2.target === "a");
  check("stock/stock booted B -> system_b stamped", r2.systemPart === 14);
  const r3 = chooseBootSlots(classifyBootSlots(probe("stock", "stock")), "");
  check("stock/stock, slot unknown -> still decides", r3.ok && r3.donor === "a");
}
{
  // The refusal, and it is the state every device the OLD rule touched is in.
  const r = chooseBootSlots(classifyBootSlots(probe("ours", "ours")), "_a");
  check("ours/ours refuses", !r.ok);
  check("ours/ours says how to recover", /escrowed boot image/.test(r.reason));
}
{
  const r = chooseBootSlots(classifyBootSlots(probe("empty", "empty")), "_a");
  check("empty/empty refuses", !r.ok);
}

// ── things that must not become a write ─────────────────────────────────────
{
  // A system partition we cannot resolve means we cannot stamp the image, and
  // an unstamped image silently falls back to p13 — which is the WRONG
  // userspace whenever the donor is B. Refuse rather than build it.
  const p = `SLOT a stock /dev/block/mmcblk0p10\nSLOT b empty /dev/block/mmcblk0p11`;
  const r = chooseBootSlots(classifyBootSlots(p), "_a");
  check("no system map -> refuses rather than guessing", !r.ok);
}
{
  const p = `SLOT a stock /dev/block/mmcblk0p10\nSLOT b empty\n${SYS}`;
  const r = chooseBootSlots(classifyBootSlots(p), "_a");
  check("target with no block device -> refuses", !r.ok);
}
{
  // An unknown state is not a licence to write there.
  const r = chooseBootSlots(classifyBootSlots(probe("stock", "weird")), "_a");
  check("unknown target state still writes only the non-stock slot",
        r.ok && r.target === "b");
  const r2 = chooseBootSlots(classifyBootSlots(probe("weird", "weird")), "_a");
  check("no stock anywhere refuses whatever the states say", !r2.ok);
}
{
  // The donor is never the target. Nothing else in the wizard re-checks this.
  for (const [a, b] of [["stock","ours"],["ours","stock"],["stock","empty"],["stock","stock"]]) {
    const r = chooseBootSlots(classifyBootSlots(probe(a, b)), "_a");
    if (r.ok && r.donor === r.target) { console.log(`FAIL  donor===target for ${a}/${b}`); fails++; }
  }
  check("donor is never the target", true);
}

console.log(fails ? "\nFAILED" : "\nall ok");
process.exit(fails ? 1 : 0);
