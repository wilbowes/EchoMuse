// Tests for _preseedVerdict() in dashboard.jsx — a wizard step that wrote a
// file and reported success without reading it back.
//
//     node controller/tests/artifact_verdict.test.mjs
//
// Source extraction rather than import, for the reason pm_verdict.test.mjs
// gives: the dashboard compiles to a single classic script with no module
// boundary, so the alternative is a second copy that drifts.
//
// The rule under it: **a probe's results are evidence, not proof.** A command
// that exited 0 proves it ran, not that it did what the step needed:
//
//   #267  magisk.db measured at 0 bytes where it should be 36864, `cp &&`
//         chmod both returning 0, and magiskd then refusing every su with
//         "no such table: policies". push() returns without draining, so the
//         copy can read the file before cat has flushed it.
//
// The failure is silent on hardware nobody is watching a log of, and it
// looks like a working device.

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

const { _preseedVerdict } = await import(
  "data:text/javascript;base64," + Buffer.from(
    liftArrow("_preseedVerdict")
    + "\nexport { _preseedVerdict };"
  ).toString("base64"));

let failures = 0;
function check(name, cond, detail) {
  if (cond) return;
  failures++;
  console.error(`FAIL: ${name}${detail ? `\n      ${detail}` : ""}`);
}

// ── magisk.db ────────────────────────────────────────────────────────────────
// DB=<bytes> is the size of the INSTALLED file, read off the device; _DBCHK
// says the probe ran. 36864 is the size the reported device was short of.
// A null is the probe answering with no size at all — the file missing, or the
// tool reading it missing.
const WANT = 36864;
const db = (n) => (n === null ? "" : `DB=${n}\n`) + "_DBCHK";

{
  const v = _preseedVerdict(db(WANT), WANT);
  check("a magisk.db of the served size proceeds", v.ok, JSON.stringify(v));
}

for (const [what, out] of [
  ["the copy landed 0 bytes", db(0)],
  ["the copy landed short", db(1024)],
  // Longer is wrong too — that is not the DB the controller served, and
  // matching on "at least as big" would hide a truncated or concatenated file.
  ["the copy landed more than was sent", db(WANT + 1)],
  ["the file is not there at all", db(null)],
  ["the probe answered but reported no size", "_DBCHK"],
  ["the probe produced no output at all", ""],
  ["the probe was cut off before its answer", "DB=36864"],
]) {
  const v = _preseedVerdict(out, WANT);
  check(`${what} aborts`, v.ok === false, `ok=${v.ok}: ${v.why}`);
}

{
  const v = _preseedVerdict(db(0), WANT);
  check("the failure reports both numbers, measured and expected",
        /\b0\b/.test(v.why) && v.why.includes(String(WANT)), v.why);
  check("the failure names the symptom magiskd produces",
        /no such table/.test(v.why), v.why);
}

// A 0-byte DB is the bug, so it cannot be the success case either — and the
// controller answering a provision request with an empty body is the one way
// dbBytes.length is 0. Refuse it as what it is rather than verify against it.
{
  const v = _preseedVerdict(db(0), 0);
  check("a 0-byte expected DB is refused, not verified against",
        v.ok === false, JSON.stringify(v));
  check("and says the controller served nothing",
        /empty|no bytes|0 bytes/i.test(v.why), v.why);
}

// Missing or nonsense arguments must not throw — a verdict that raises is a
// step that dies without a sentence.
for (const fn of [(out) => _preseedVerdict(out, WANT)]) {
  for (const arg of [undefined, null, ""]) {
    let threw = null;
    try { fn(arg); } catch (e) { threw = e; }
    check(`${fn.name}(${JSON.stringify(arg)}) does not throw`,
          threw === null, threw?.message);
  }
}
check("no probe at all is not a pass", _preseedVerdict(undefined, WANT).ok === false);

// ── The call sites ───────────────────────────────────────────────────────────
// A verdict the step never asks for is dead code, and a source check cannot tell
// an abort from a log line that reads like one. So both steps are DRIVEN, the
// way boot_target.test.mjs drives runPatchBoot: lifted whole, free references
// injected, an in-memory device in place of adb. Booting the dashboard is not
// possible here, so lifting is also the syntax check for the edits.
const fnBody = (name) => {
  const start = src.indexOf(`async function ${name}(`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}()`);
  let depth = 0;
  let i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) break; }
  }
  return src.slice(start, i + 1);
};

// #267: a device whose magisk.db landed 0 bytes,
// which is what the reported device measured against a healthy controller.
{
  const drive = async (onDevice, { ran = true } = {}) => {
    const logs = [];
    let error = null;
    const c = {
      // The answer is built by RUNNING the probe's own format rather than
        // by repeating what _preseedVerdict wants to read. A stub that
        // hardcodes `DB=` tests the verdict against itself and stays green
        // while the real command prints something else — which is exactly
        // how the first version shipped a step that refused every device.
        async shell(command) {
        if (!command.includes("_DBCHK")) return "";
        const m = command.match(/DB=\$\(wc -c/);
        if (!m) throw new Error("probe no longer emits the DB= label the verdict parses");
        return (onDevice === null ? "DB=\n" : `DB=${onDevice}\n`) + (ran ? "_DBCHK" : "");
      },
      async push() {},
    };
    const runPreseedDb = new Function("addLog", "fetch", "ingressPath", "token",
      "_preseedVerdict", `return ${fnBody("runPreseedDb")}`)(
        text => logs.push(text),
        async () => ({ ok: true, arrayBuffer: async () => new Uint8Array(WANT) }),
        path => path, "", _preseedVerdict);
    try { await runPreseedDb(c); } catch (e) { error = e; }
    return { logs, error };
  };

  const good = await drive(WANT);
  check("a magisk.db of the served size installs", good.error === null, good.error?.message);
  check("and is logged as installed", good.logs.includes("magisk.db installed."),
        good.logs.join(" | "));
  check("and the log says what was pushed",
        good.logs.includes(`magisk.db: ${WANT} bytes`), good.logs.join(" | "));

  for (const [what, size, opts] of [
    ["0 bytes", 0, {}],
    ["a short file", 1024, {}],
    ["no file at all", null, {}],
    ["a probe that never ran", 0, { ran: false }],
  ]) {
    const r = await drive(size, opts);
    check(`a DB at ${what} aborts the step`, r.error !== null, "installed anyway");
    // The logged success IS the reported symptom.
    check(`a DB at ${what} is never logged as installed`,
          !r.logs.includes("magisk.db installed."), r.logs.join(" | "));
  }

  const zero = await drive(0);
  check("the refusal reports the measured and the expected size",
        /\b0\b/.test(zero.error?.message || "")
          && zero.error.message.includes(String(WANT)), zero.error?.message);
}

// The FireOS start script, installed into Magisk's service.d by the same
// push-then-cp. An empty one is a device that never comes back after the
// post-install reboot, so the step reads the file back and compares it whole.
{
  const SCRIPT = "#!/system/bin/sh\nstart\n";
  const drive = async (onDevice) => {
    const logs = [];
    let error = null;
    const c = {
      async shell(command) {
        return command.includes("_SVCCHK") ? `${onDevice}_SVCCHK\r\n` : "";
      },
      async push() {},
    };
    const run = new Function("addLog", "_SERVICE_D_SCRIPT",
      `return ${fnBody("runInstallStartScript")}`)(text => logs.push(text), SCRIPT);
    try { await run(c); } catch (e) { error = e; }
    return { logs, error };
  };

  const good = await drive(SCRIPT.replace(/\n/g, "\r\n"));
  check("a start script that reads back whole installs", good.error === null,
        good.error?.message);
  for (const [what, onDevice] of [
    ["empty", ""],
    ["cut short", SCRIPT.slice(0, 10)],
    ["something else", "#!/system/bin/sh\n"],
  ]) {
    const r = await drive(onDevice);
    check(`a start script read back ${what} aborts the step`, r.error !== null,
          "installed anyway");
  }
}

// The probe and the verdict must agree on the markers, or one of them is
// testing a string the device never prints. Comments stripped first: a guard
// that greps for the thing it forbids otherwise finds the comment explaining
// why and passes — the mistake this repo has made three times.
const code = src
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/^[ \t]*\/\/.*$/gm, "");
check("runPreseedDb's probe prints the sentinel both ends agree on",
      code.includes("echo _DBCHK") && liftArrow("_preseedVerdict").includes("_DBCHK"));

if (failures) {
  console.error(`\n${failures} check(s) failed.`);
  process.exit(1);
}
console.log("artifact_verdict: all checks passed.");