// Tests for _wizardCancelable() in dashboard.jsx — when Cancel is withheld
// during a wizard step (#269).
//
//     node controller/tests/wizard_cancel.test.mjs
//
// The wizard drives hardware nobody is watching a log of, and every rule in it
// fails SILENTLY when broken. This one matters because the failure is
// destructive: abandoning a partition write part-way leaves a device whose
// boot partition is in an unknown state, and the readback that proves a write
// was clean cannot run against a cancelled step. A control that offers it
// anyway is a control that costs somebody a TWRP session.
//
// The helper is LIFTED and RUN rather than pattern-matched, so the assertions
// are about what it returns for a given step list.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

function liftFunction(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}()`);
  // liftBlock finds the body braces; the declaration before them is the
  // signature, so the slice starts at `function` and runs to the closing brace.
  let depth = 0;
  for (let i = src.indexOf("{", start); i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`could not find the end of ${name}`);
}

function liftConst(name) {
  const start = src.indexOf(`const ${name} = [`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}`);
  // Match on the ARRAY's brackets, not braces: the step objects are inside
  // it, so counting braces would stop at the end of the first entry and emit
  // a truncated list that parses and is wrong.
  let depth = 0;
  for (let i = src.indexOf("[", start); i < src.length; i++) {
    if (src[i] === "[") depth++;
    else if (src[i] === "]" && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`could not find the end of ${name}`);
}

const { _wizardCancelable, _WIZARD_STEPS, _EMOS_STEPS } = await import(
  "data:text/javascript;base64," + Buffer.from(
    [liftConst("_WIZARD_STEPS"), liftConst("_EMOS_STEPS")].join("\n")
    + "\n" + liftFunction("_wizardCancelable")
    + "\nexport { _wizardCancelable, _WIZARD_STEPS, _EMOS_STEPS };"
  ).toString("base64"));

let failures = 0;
function check(name, got, want) {
  if (got === want) return;
  failures++;
  console.error(`FAIL: ${name}\n      got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

// Every step that writes a partition is protected. Named explicitly, so
// adding a write step without the flag is visible in this list.
for (const id of ["patch_boot", "install_magisk", "flash_emos"]) {
  check(`${id} cannot be cancelled`, _wizardCancelable(id), false);
}

// Everything else stays cancelable, which is the point of withholding it
// narrowly: Cancel is the only way out of a step that stalls, and losing it
// across the whole wizard would replace a destructive option with a dead end.
for (const id of ["connect_android", "connect_twrp", "escrow_boot", "build_emos",
                  "reboot_watch", "wifi_register", "install_em", "install_oww",
                  "preseed_db", "reboot", "reconnect", "verify_root",
                  "disable_alexa", "debloat", "wifi"]) {
  check(`${id} can be cancelled`, _wizardCancelable(id), true);
}

// No step is left without an answer, and none is protected by accident.
for (const list of [_WIZARD_STEPS, _EMOS_STEPS]) {
  for (const step of list) {
    if (typeof _wizardCancelable(step.id) !== "boolean") {
      failures++;
      console.error(`FAIL: ${step.id} has no cancel answer`);
    }
  }
}

// An unknown id must not be treated as a write. A step added to the list
// without a flag has to be cancelable until someone says otherwise — the
// asymmetry is deliberate: offering Cancel wrongly is recoverable, withholding
// it wrongly is a device that needs a cable.
check("an unknown step is cancelable", _wizardCancelable("some_new_step"), true);
check("no step at all is cancelable", _wizardCancelable(undefined), true);
check("an empty id is cancelable", _wizardCancelable(""), true);

// The gate has to be reached, not just defined. A helper nothing calls is a
// helper that documents an intention.
if (!/\{running && _wizardCancelable\(cur\.id\)/.test(src)) {
  failures++;
  console.error(
    "FAIL: Cancel is not gated\n      the button still renders on `running &&` " +
    "alone — _wizardCancelable is defined but not consulted, so nothing changed (#269)"
  );
}

// ...and withholding it silently is its own failure: a control that vanishes
// is indistinguishable from one that is broken.
if (!/\{running && !_wizardCancelable\(cur\.id\)/.test(src)) {
  failures++;
  console.error(
    "FAIL: the reason is never shown\n      Cancel is withheld with nothing to " +
    "say why — which reads as a broken control, and this one is asked about often"
  );
}

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("wizard_cancel: all checks passed");
