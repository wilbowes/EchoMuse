// Tests for the Activity panel's wake-sample retention title and page size in
// dashboard.jsx — what the panel claims and asks for.
//
//     node controller/tests/wake_keep.test.mjs
//
// Source extraction rather than import, for the reason pm_verdict.test.mjs
// gives: the dashboard compiles to a single classic script with no module
// boundary, so the alternative is a second copy that drifts.
//
// The rule under test is that both come from the same clamp as em_db's:
// newest 50 by default, and never a number the API would refuse.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import assert from "assert";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

function lift() {
  const f = src.match(/const wakeKeepFor = v => \{([\s\S]*?)\n\};/);
  const t = src.match(/const wakeKeepTitle = v => (.+);\n/);
  if (!f || !t) {
    throw new Error(
      "dashboard.jsx no longer defines wakeKeepFor/wakeKeepTitle — if they " +
      "were renamed, update this test; if the title moved, make sure it still " +
      "renders the effective retention rather than a fixed number.");
  }
  const keep = new Function("v", f[1]);
  const title = new Function("wakeKeepFor", "v", `return ${t[1]};`).bind(null, keep);
  return { keep, title };
}

const { keep, title } = lift();

// ── The clamp and the default ────────────────────────────────────────────────

assert.strictEqual(keep(undefined), 50, "an absent value is the 50 default");
assert.strictEqual(keep(null), 50, "a null value is the 50 default");
assert.strictEqual(keep(9), 10, "below the floor clamps to 10");
assert.strictEqual(keep(10), 10, "the floor itself is allowed");
assert.strictEqual(keep(500), 500, "the ceiling itself is allowed");
assert.strictEqual(keep(501), 500, "above the ceiling clamps to 500");
assert.strictEqual(keep(137), 137, "a value inside the range is kept as stored");
assert.strictEqual(keep(12.9), 12, "a fractional value truncates to an integer");

// ── The title follows the value ──────────────────────────────────────────────

assert.strictEqual(title(undefined), "Wake-word samples · newest 50");
assert.strictEqual(title(137), "Wake-word samples · newest 137");
assert.strictEqual(title(501), "Wake-word samples · newest 500");

// ── The panel asks for what it says ──────────────────────────────────────────

assert.ok(/wakeKeepTitle\(wakeClipKeep\)/.test(src),
  "the panel title must render the effective retention");
assert.ok(/limit=\$\{keep\}/.test(src),
  "the panel must ask for the same count it titles");

console.log("wake_keep: all ok");
