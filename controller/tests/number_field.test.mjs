// Tests for NumberField's input filtering in dashboard.jsx — what a typed
// string is allowed to become.
//
//     node controller/tests/number_field.test.mjs
//
// Source extraction rather than import, for the reason pm_verdict.test.mjs
// gives: the dashboard compiles to a single classic script with no module
// boundary, so the alternative is a second copy that drifts.
//
// The rule under test is that 0 must be reachable ONLY by typing it. This
// control is the console idle timeout, where 0 means NEVER — so a fractional
// entry that rounds to 0 does not produce a short timeout, it silently turns
// the timeout off. `0.1` is the entry somebody makes when they want the
// shortest possible one, which is precisely the opposite. Rounding looks like
// the obvious way to enforce integers and has that hole in it; stripping
// non-digits as they are typed does not.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import assert from "assert";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

// Lift the one-line `digits` helper out of NumberField.
function liftDigits() {
  const m = src.match(/const digits = \(s\) => ([^;]+);/);
  if (!m) {
    throw new Error(
      "dashboard.jsx no longer defines NumberField's digits() helper — if it " +
      "was renamed, update this test; if the filtering moved, make sure 0 is " +
      "still reachable only by typing it.");
  }
  return new Function("s", `return ${m[1]};`);
}

const digits = liftDigits();

// The commit path, mirrored from the component: filter, then clamp.
const commit = (raw, min, max) => {
  const d = digits(raw);
  if (d === "") return null;                 // still typing; nothing written
  return Math.min(max, Math.max(min, Number(d)));
};

// ── The hazard this exists for ───────────────────────────────────────────────

// Round(0.1) is 0 and 0 means "never". Stripping gives 1: the shortest
// timeout, which is what was being asked for.
assert.strictEqual(commit("0.1", 0, 90), 1,
  "a fractional entry must not become 0 — 0 means the timeout is off");

assert.strictEqual(commit("0.9", 0, 90), 9, "no rounding, digits only");

// The only way to 0.
assert.strictEqual(commit("0", 0, 90), 0, "typing 0 means 0");

// ── Integers only ────────────────────────────────────────────────────────────

assert.strictEqual(commit("7.5", 0, 90), 75,
  "the decimal point is dropped rather than rounded");
// Not 1000-clamped-to-90: the 'e' goes and the remaining digits are read as
// written. Either way the point holds — exponent notation cannot survive, and
// what lands is a plain integer.
assert.strictEqual(commit("1e3", 0, 90), 13, "exponent notation cannot survive");
assert.strictEqual(commit("-5", 0, 90), 5, "a minus sign is not a digit");
assert.strictEqual(commit("12abc", 0, 90), 12, "letters are dropped");
assert.strictEqual(commit(" 20 ", 0, 90), 20, "whitespace is dropped");

// ── Clamping, not rejecting ──────────────────────────────────────────────────
//
// An out-of-range entry becomes the nearest legal value. Refusing it instead
// leaves an error nobody can act on next to a box that still shows their
// number.

assert.strictEqual(commit("200", 0, 90), 90, "above max clamps to max");
assert.strictEqual(commit("90", 0, 90), 90, "max itself is allowed");

// ── Empty is "still typing", never a write ───────────────────────────────────
//
// Clearing the box to type a new number must not commit 0 on the way past.

assert.strictEqual(commit("", 0, 90), null, "an empty box writes nothing");

// The device parser agrees with all of the above: it reads digits and stops at
// anything else, so a fraction would land there as 0 whatever we sent. Pin
// that the two are still describing the same thing.
const init = readFileSync(
  join(HERE, "..", "..", "emos", "init", "init.c"), "utf8");
assert.ok(/console_timeout_secs/.test(init),
  "emos/init/init.c no longer has console_timeout_secs — the device half of " +
  "this contract moved, so check the box still cannot send it a fraction");

console.log("number_field: all ok");
