// Tests for the wake-word Sensitivity slider's mapping in dashboard.jsx — the
// set of thresholds the control can actually produce.
//
//     node controller/tests/wake_sensitivity.test.mjs
//
// Source extraction rather than import, for the reason pm_verdict.test.mjs
// gives: the dashboard compiles to a single classic script with no module
// boundary, so the alternative is a second copy that drifts.
//
// The rule under test is that EVERY reachable threshold must be one a real
// wake can clear. openwakeword's score is a sigmoid that saturates below 1.0
// and both scorers test `score >= threshold`, so a threshold of exactly 1.0 is
// a bar nothing clears. The previous 1..9 mapping produced exactly that at
// position 1: the most precise notch was a setting that could never wake, and
// it presented as "the Echo stopped responding" rather than as a bad value.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import assert from "assert";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

// Lift the range constants and the reflection helper out of the config panel.
function liftMapping() {
  const c = src.match(
    /const WAKE_T_MIN = ([\d.]+), WAKE_T_MAX = ([\d.]+), WAKE_T_STEP = ([\d.]+);/);
  const f = src.match(/const reflectT = t => ([^;]+);/);
  if (!c || !f) {
    throw new Error(
      "dashboard.jsx no longer defines the Sensitivity range constants or " +
      "reflectT — if they were renamed, update this test; if the mapping " +
      "moved, make sure no reachable threshold is >= 1.0.");
  }
  const MIN = Number(c[1]), MAX = Number(c[2]), STEP = Number(c[3]);
  const body = new Function("WAKE_T_MIN", "WAKE_T_MAX", "t", `return ${f[1]};`);
  const reflectT = (t) => body(MIN, MAX, t);

  const w = src.match(/const wakeTrackFor = t => ([^;]+);/);
  if (!w) {
    throw new Error(
      "dashboard.jsx no longer defines wakeTrackFor — a threshold stored " +
      "outside the range has to be pinned deliberately, or the range input " +
      "clamps it silently and the readout flatters a device that cannot wake.");
  }
  const wakeTrackFor = new Function(
    "WAKE_T_MIN", "WAKE_T_MAX", "reflectT", "t", `return ${w[1]};`)
    .bind(null, MIN, MAX, reflectT);

  return { MIN, MAX, STEP, reflectT, wakeTrackFor };
}

const { MIN, MAX, STEP, reflectT, wakeTrackFor } = liftMapping();

// Every notch the slider can land on, as the threshold it writes to config.
const notches = [];
for (let i = 0; ; i++) {
  const track = Number((MIN + i * STEP).toFixed(10));
  if (track > MAX + 1e-9) break;
  notches.push(reflectT(track));
}

// ── The hazard this exists for ───────────────────────────────────────────────

// A threshold of 1.0 cannot be crossed by a sigmoid that saturates below it.
// Anything >= 1.0 is a wake word that can never fire.
assert.ok(notches.every((t) => t < 1.0),
  `every reachable threshold must be < 1.0 — got ${notches.filter((t) => t >= 1.0)}`);

// The other half of the same bug: the eager end must stay usable too, so the
// track cannot run off into thresholds so low that ordinary speech wakes it.
assert.ok(notches.every((t) => t > 0), "no reachable threshold may be <= 0");

// ── The range is worth having ────────────────────────────────────────────────

// The precise end must reach past 0.9. That is the whole point: 0.9 was the
// strictest USABLE setting under the old mapping, so a fix that stops there
// gives an operator who is already at 0.9 nowhere to go.
assert.ok(Math.max(...notches) > 0.9,
  "the precise end must offer thresholds stricter than 0.9");

// ── The mapping is sound ─────────────────────────────────────────────────────

// reflectT converts both ways, so the displayed value and the stored value
// cannot disagree.
for (const t of [MIN, MAX, 0.5, 0.975, 0.125]) {
  assert.ok(Math.abs(reflectT(reflectT(t)) - t) < 1e-9,
    `reflectT must be its own inverse, failed at ${t}`);
}

// The track's ends are the range's ends, inverted: leftmost is most precise.
assert.strictEqual(reflectT(MIN), MAX, "the left end of the track is the strictest threshold");
assert.strictEqual(reflectT(MAX), MIN, "the right end of the track is the most eager threshold");

// An exact number of steps, so no notch lands on a float artefact like
// 0.30000000000000004 and every position is a round number in the readout.
const steps = (MAX - MIN) / STEP;
assert.ok(Math.abs(steps - Math.round(steps)) < 1e-9,
  `the range must be a whole number of steps — got ${steps}`);

// The shipped default has to be reachable by dragging, or the control cannot
// return to it once moved. Read from em_db.py rather than repeated here: a
// default quietly moved off the grid is the same class of bug as the one this
// file exists for, and a literal would keep passing through it.
const db = readFileSync(join(HERE, "..", "em_db.py"), "utf8");
const dm = db.match(/"owwThreshold":\s*([\d.]+),/);
if (!dm) {
  throw new Error(
    "em_db.py no longer defines an owwThreshold default where this test reads " +
    "it — if DEFAULTS moved, point this at the new home; the default must " +
    "still land on a notch.");
}
const DEFAULT = Number(dm[1]);
assert.ok(DEFAULT >= MIN && DEFAULT <= MAX,
  `the ${DEFAULT} default must be inside the slider's range`);
const offGrid = (DEFAULT - MIN) / STEP;
assert.ok(Math.abs(offGrid - Math.round(offGrid)) < 1e-9,
  `the ${DEFAULT} default must land on a notch`);

// ── Thresholds stored before this range existed ──────────────────────────────

// The old slider could write exactly 1.0, so fielded configs carry it. The
// handle has to be pinned rather than left to the range input, which clamps
// out-of-range values to min silently.
assert.strictEqual(wakeTrackFor(1.0), MIN,
  "a legacy 1.0 threshold must pin the handle at the precise end");
for (const legacy of [1.0, 1.5, 0.0, -1]) {
  const track = wakeTrackFor(legacy);
  assert.ok(track >= MIN && track <= MAX,
    `a stored ${legacy} must land on the track, got ${track}`);
}

// ...and the readout must keep showing what is STORED, not where the handle
// ended up, or 1.0 reads back as 0.975: a plausible number for a device that
// cannot wake. Pinned as source shape, since the value is rendered by Slider.
assert.ok(/formatValue=\{\(\) => wakeT\.toFixed\(3\)\}/.test(src),
  "the Sensitivity readout must render the stored threshold, not the clamped " +
  "track position — otherwise a device stored at 1.0 displays as 0.975");

// ── The contract with the scorers ────────────────────────────────────────────

// Both of these use `>=`, which is what makes a 1.0 threshold unreachable
// rather than merely strict. If either flips to `>`, the reasoning above needs
// revisiting — and if either stops comparing a score to a threshold at all,
// this control is no longer setting what this test thinks it sets.
const ctrl = readFileSync(join(HERE, "..", "em_controller.py"), "utf8");
assert.ok(/score >= eff_threshold/.test(ctrl),
  "em_controller.py no longer tests `score >= eff_threshold` — re-check whether " +
  "the top of the Sensitivity range is still reachable");

const shadow = readFileSync(
  join(HERE, "..", "..", "device", "internal", "wakeword", "shadow", "shadow.go"), "utf8");
assert.ok(/score >= threshold/.test(shadow),
  "shadow.go no longer tests `score >= threshold` — re-check whether the top " +
  "of the Sensitivity range is still reachable on-device");

// ── Barge threshold runs the same way ────────────────────────────────────────

// Every threshold slider on the form reads Precise -> Eager (2026-09-19), so a
// barge slider back on a plain low -> high track is a regression even though
// it "works".
{
  const m = src.match(/const BARGE_T_MIN = ([\d.]+), BARGE_T_MAX = ([\d.]+), BARGE_T_STEP = ([\d.]+);/);
  assert.ok(m, "dashboard.jsx no longer defines the barge threshold range constants");
  const [bMin, bMax, bStep] = m.slice(1).map(Number);
  const reflect = t => Number((bMin + bMax - t).toFixed(3));
  for (let v = bMin; v <= bMax + 1e-9; v = Number((v + bStep).toFixed(3))) {
    assert.ok(Math.abs(reflect(reflect(v)) - v) < 1e-9, `barge reflection is not its own inverse at ${v}`);
  }
  assert.ok(/onChange=\{v => set\('bargeInThreshold', reflectBarge\(v\)\)\}/.test(src),
    "the barge slider must write the REFLECTED track value, so left is precise");
  assert.ok(/formatValue=\{\(\) => bargeT\.toFixed\(2\)\}/.test(src),
    "the barge readout must render the stored threshold, not the track position");
}

console.log(`wake_sensitivity: all ok (${notches.length} notches, ` +
  `${Math.min(...notches)}..${Math.max(...notches)})`);
