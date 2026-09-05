// Tests for the transcript line classifier in dashboard.jsx.
//
//     node controller/tests/wizard_log_class.test.mjs
//
// Same source-extraction approach as wifi_scan.test.mjs: the function is
// lifted out of dashboard.jsx rather than imported, because the dashboard
// compiles to a single classic script. If the function is renamed or moved,
// the lift fails loudly and this file must follow.
//
// _wizardLogClass is what decides whether an error line gets the --error
// styling that makes it visible when a provisioning run goes wrong. An
// "Error:" line that misses the regex falls through to the default --info
// and disappears into exactly the noise it was supposed to be pulled out of.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

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

const code = [
  liftFunction("_wizardLogClass"),
  "export { _wizardLogClass };",
].join("\n");

const { _wizardLogClass } = await import(
  "data:text/javascript;base64," + Buffer.from(code).toString("base64"));

let failures = 0;
function check(label, got, want) {
  const ok = got === want;
  if (!ok) {
    failures++;
    console.error(`FAIL  ${label}\n      got  ${got}\n      want ${want}`);
  } else {
    console.log(`ok    ${label}`);
  }
}

// Explicit types are authoritative over content regexes.
check("head type wins", _wizardLogClass("anything", "head"), "em-console__line--head");
check("error type wins", _wizardLogClass("anything", "error"), "em-console__line--error");
check("ok type wins", _wizardLogClass("anything", "ok"), "em-console__line--ok");
check("warn type wins", _wizardLogClass("anything", "warn"), "em-console__line--warn");

// The content regexes for the default (no type) route — these are what
// actually run on transcript lines that come in untyped.
check("Error: prefix is error", _wizardLogClass("Error: shell died", null), "em-console__line--error");
check("lowercase error is not an error line", _wizardLogClass("error: something", null), "em-console__line--info");
check("adbcopy prefix is adb", _wizardLogClass("  adb: 0101", null), "em-console__line--adb");
check("waiting prefix is wait", _wizardLogClass("Waiting for device", null), "em-console__line--wait");
check("still waiting prefix is wait", _wizardLogClass("still waiting on su", null), "em-console__line--wait");
check("elapsed counter prefix is wait", _wizardLogClass("  [15s] boot_completed=0", null), "em-console__line--wait");
check("detail arrow is detail", _wizardLogClass("  → muted", null), "em-console__line--detail");
check("detached arrow is not detail", _wizardLogClass("  -> not an arrow", null), "em-console__line--info");
check("plain line is info", _wizardLogClass("ordinary transcript", null), "em-console__line--info");
check("empty message is info", _wizardLogClass("", null), "em-console__line--info");

// The regression the maintainer flagged: an Error: line must never fall
// through to --info. The regex anchors only "^Error:", so a leading non->
// prefix correctly stays --info; make sure the anchored case is covered.
check("multi-line error block keeps first-line class",
      _wizardLogClass("Error: cannot flash", null),
      "em-console__line--error");

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
