// Tests for the pure piece behind the wizard's failure panel.
//
//     node controller/tests/wizard_step_failure.test.mjs
//
// Lifted out of dashboard.jsx as wizard_log_class.test.mjs does.
//
// _stepFailureReason is what the panel shows as the reason; on
// 2026-10-11 the panel said only "Click Retry first" for a missing file that
// no retry could supply.

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

const code = [
  liftFunction("_stepFailureReason"),
  "export { _stepFailureReason };",
].join("\n");

const { _stepFailureReason } = await import(
  "data:text/javascript;base64," + Buffer.from(code).toString("base64"));

let failures = 0;
function check(label, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    failures++;
    console.error(`FAIL  ${label}\n      got  ${g}\n      want ${w}`);
  } else {
    console.log(`ok    ${label}`);
  }
}

check("first paragraph only",
  _stepFailureReason("f1r30s.zip is not on this Echo.\n\nunzip said:\ncan't open"),
  "f1r30s.zip is not on this Echo.");
check("single paragraph kept whole", _stepFailureReason("  It failed. "), "It failed.");
check("blank line with spaces still splits", _stepFailureReason("A\n  \nB"), "A");
check("nothing in, nothing out", _stepFailureReason(undefined), "");

if (failures) { console.error(`\n${failures} failed`); process.exit(1); }
console.log("\nall passed");
