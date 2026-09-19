// Tests for _wipeVerdict() in dashboard.jsx — whether the wizard's optional
// factory reset (`twrp wipe data` + `twrp wipe cache`) actually happened.
//
//     node controller/tests/wipe_verdict.test.mjs
//
// Source extraction rather than import, for the reason pm_verdict.test.mjs
// gives: the dashboard compiles to a single classic script with no module
// boundary, so the alternative is a second copy that drifts.
//
// Neither twrp command's exit status means anything, so this verdict is the
// only thing between "Wiped." and a device that kept its old /data. The rule
// under test: a probe that could not run, or ran against an unmounted /data,
// is never a pass — `[ -e ]` is false for everything on an empty mountpoint.

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

const { _wipeVerdict } = await import(
  "data:text/javascript;base64," + Buffer.from(
    liftArrow("_wipeVerdict") + "\nexport { _wipeVerdict };"
  ).toString("base64"));

let failures = 0;
function check(name, cond, detail) {
  if (cond) return;
  failures++;
  console.error(`FAIL: ${name}${detail ? `\n      ${detail}` : ""}`);
}

const probe = ({ data = "1", left = "", cache = "lost+found ", end = true } = {}) =>
  `DATA=${data}\r\nLEFT=${left}\r\nCACHE=${cache}\r\n` + (end ? "_WIPECHK\r\n" : "");

// ── Passes ──
const clean = _wipeVerdict(probe());
check("a clean wipe passes", clean.ok, JSON.stringify(clean));
check("lost+found in /cache is not reported", clean.cacheLeft.length === 0);
check("TWRP's own recovery/ in /cache is not reported",
  _wipeVerdict(probe({ cache: "lost+found recovery " })).cacheLeft.length === 0);

const stale = _wipeVerdict(probe({ cache: "lost+found backup dalvik-cache " }));
check("leftover /cache warns but does not fail", stale.ok);
check("leftover /cache is named",
  stale.cacheLeft.join(",") === "backup,dalvik-cache", JSON.stringify(stale.cacheLeft));

// ── Failures ──
check("a probe that never finished is not a pass", !_wipeVerdict(probe({ end: false })).ok);
check("empty output is not a pass", !_wipeVerdict("").ok);
check("an unmounted /data is not a pass, even with nothing left",
  !_wipeVerdict(probe({ data: "0" })).ok);
check("an unreadable mount count is not a pass", !_wipeVerdict(probe({ data: "" })).ok);

const left = _wipeVerdict(probe({ left: " system local" }));
check("anything left in /data fails", !left.ok);
check("the failure names what was left",
  left.why.includes("/data/system") && left.why.includes("/data/local"), left.why);

// ── The probe and the verdict agree on the markers ──
const runner = src.slice(src.indexOf("async function runWipeData"),
                         src.indexOf("async function runWipeData") + 3000);
for (const m of ["DATA=", "LEFT=", "CACHE=", "_WIPECHK"]) {
  check(`runWipeData's probe prints ${m}`, runner.includes(m));
}
check("runWipeData decides with _wipeVerdict", /_wipeVerdict\(probe\)/.test(runner));

// ── It runs where it must: inside step 1, in the emOS flow only ──
// On FireOS 5 a data wipe takes f1r30s's state with it and the wizard does
// not reinstall it, so the FireOS flow must never reach runWipeData.
const calls = src.match(/runConnectTwrp\(\);\s*\n\s*if \(wipeData\) await runWipeData\(c\);/g) || [];
check("exactly one flow runs the wipe straight after Connect to TWRP", calls.length === 1,
  `found ${calls.length}`);
const emosSwitch = src.slice(src.indexOf("if (isEmos) switch (stepIdx)"),
                             src.indexOf("else switch (stepIdx)"));
check("that flow is emOS", /if \(wipeData\) await runWipeData\(c\)/.test(emosSwitch));
check("runWipeData is called from nowhere else",
  (src.match(/await runWipeData\(/g) || []).length === 1);
check("switching to FireOS clears the option",
  /if \(next !== 'emos'\) setWipeData\(false\);/.test(src));
check("the option defaults to off", /useState\(false\);\s*$/m.test(
  src.slice(src.indexOf("const [wipeData, setWipeData]"),
            src.indexOf("const [wipeData, setWipeData]") + 60)));

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("wipe_verdict: all checks passed");
