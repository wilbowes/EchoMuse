// Tests for resumeStepFrom() in dashboard.jsx — the rule that decides where the
// provisioning wizard goes back to after a reconnect.
//
//     node controller/tests/resume_step.test.mjs
//
// Source extraction rather than import, for the reason boot_target.test.mjs
// gives: the dashboard compiles to one classic script with no module boundary.
//
// What is under test is the wizard's own memory of a device it is not watching.
// The Dot's only power source is the micro-USB port carrying data, so pulling
// the cable powers it off, and `reboot recovery` is a one-shot BCB flag, so a
// replug is a cold boot into Android whatever phase the wizard thinks it is in.
// A pointer that survived that is a pointer that will run four steps against a
// device that has had them for ten minutes, with nothing on screen saying so.
//
// So the two directions are not symmetric and the tests do not treat them as
// such. Undershooting costs a step that re-runs and finds nothing to do.
// Overshooting drops a step nobody ran — and one of them is a boot-partition
// write — so nothing here may skip a step whose evidence is short of a
// positive reading, and nothing here may decide that a step needs REDOING.

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
  // Brace counting starts at the body's opening brace, not the first `{` after
  // the name: resumeStepFrom takes a destructured parameter, and counting from
  // there stops the lift on the `}` that closes the pattern.
  let depth = 0, parens = 0, i = start;
  let body = -1;
  for (; i < src.length; i++) {
    if (src[i] === "(") parens++;
    else if (src[i] === ")") parens--;
    else if (src[i] === "{" && parens === 0 && body < 0) { body = i; depth = 1; }
    else if (body >= 0) {
      if (src[i] === "{") depth++;
      else if (src[i] === "}") { depth--; if (depth === 0) break; }
    }
  }
  return src.slice(start, i + 1);
}
function liftConst(name) {
  const at = src.indexOf(`const ${name}`);
  if (at < 0) throw new Error(`dashboard.jsx no longer defines ${name}`);
  const end = src.indexOf(";", src.indexOf("{", at));
  return src.slice(at, end + 1);
}
function liftArray(name) {
  const at = src.indexOf(`const ${name}`);
  if (at < 0) throw new Error(`dashboard.jsx no longer defines ${name}`);
  const end = src.indexOf("];", at);
  return src.slice(at, end + 2);
}

function liftList(name) {
  const at = src.indexOf(`const ${name} = [`);
  if (at < 0) throw new Error(`dashboard.jsx no longer defines ${name}`);
  return src.slice(at, src.indexOf("];", at) + 2);
}

// _ALEXA_PKGS is what the function matches package output against, so it is
// lifted rather than stubbed: a stub would make every package case below a
// statement about the harness.
const { resumeStepFrom } = await import(
  "data:text/javascript;base64," + Buffer.from(
    liftArray("_ALEXA_PKGS") + "\n" + liftConst("_RESUME_DONE")
    + "\n" + liftConst("_MODE_NAME")
    + "\n" + liftFunction("resumeStepFrom")
    + "\nexport { resumeStepFrom };"
  ).toString("base64"));

// ── the flows, as the component passes them ─────────────────────────────────
//
// The FireOS step list and its mode map are read out of the file rather than
// restated, so a step inserted, renamed or reordered upstream fails here instead
// of quietly invalidating every expectation below. Only the IDs and labels are
// parsed; `desc` is prose and carries quotes of its own.
const FIREOS = [...liftArray("_WIZARD_STEPS").matchAll(
  /\{\s*id:\s*'([^']+)'\s*,\s*label:\s*'([^']+)'/g)].map(m => ({ id: m[1], label: m[2] }));
const STEP_MODE_SRC = liftConst("_STEP_MODE");
const FIREOS_MODE = Object.fromEntries(
  [...STEP_MODE_SRC.matchAll(/(\d+):\s*'([^']+)'/g)].map(m => [+m[1], m[2]]));
const EMOS = [{ id: "connect_android", label: "Connect Device" },
              { id: "connect_twrp", label: "Connect to TWRP" },
              { id: "escrow_boot", label: "Escrow Boot Image" },
              { id: "install_em", label: "Install EchoMuse" },
              { id: "install_oww", label: "Wake Word Assets" },
              { id: "build_emos", label: "Build emOS" },
              { id: "flash_emos", label: "Flash and Verify" },
              { id: "reboot_watch", label: "Reboot and Watch" },
              { id: "wifi_register", label: "Configure WiFi" }];
const EMOS_MODE = { 0: "android", 1: "twrp", 2: "twrp", 3: "twrp", 4: "twrp", 5: "twrp",
                    6: "twrp", 7: "serial", 8: "serial" };

// The extraction has to have worked, or every case below is a statement about
// an empty list.
if (FIREOS.length < 13 || Object.keys(FIREOS_MODE).length < 13) {
  throw new Error(`could not read _WIZARD_STEPS/_STEP_MODE out of dashboard.jsx: `
                + `${FIREOS.length} steps, ${Object.keys(FIREOS_MODE).length} modes`);
}

// ── fixtures: what the probes say on a device in each state ─────────────────
//
// Shapes are the real ones. `getprop` answers `[ro.boot.selinux]: [enforce]`;
// `su -c id` answers `uid=0(root) gid=0(root)`; `pm list packages` answers
// `package:com.amazon.<name>` one per line; `wpa_cli status` answers
// `wpa_state=COMPLETED`; the server probe answers its own sentinels.
const ALexa = id => `package:${id}`;
const PKGS = (...ids) => ids.map(ALexa).join("\n");
const ONE = "com.amazon.alexa.beaconbroadcaster";

const props    = (sel) => `[ro.boot.selinux]: [${sel}]\n[ro.serialno]: [G09012345]`;
const root     = (ok) => ok ? "uid=0(root) gid=0(root)" : "/system/bin/sh: su: not found";
const services = (on) => on ? "[init.svc.echomuse]: [exited]\n[init.svc.bootanim]: [stopped]"
                            : "[init.svc.bootanim]: [stopped]";
const pm_ok    = "package:/system/framework/framework-res.apk";
const wpa      = (s) => `bssid=aa:bb:cc:dd:ee:ff\nwpa_state=${s}`;
const server   = (link, bin) => `_SERVERCHK\n${link}\n_SERVERA=${bin}`;

const ASSETS = [
  { name: "silero_vad.onnx", md5: "a".repeat(32) },
  { name: "hey_jarvis.tflite", md5: "b".repeat(32) },
  { name: "hey_mycroft.tflite", md5: "c".repeat(32) },
  { name: "alexa.tflite", md5: "d".repeat(32) },
];
const foundAll = () => Object.fromEntries(ASSETS.map(a => [a.name, a.md5]));
const foundN   = (n) => {
  const out = {};
  ASSETS.forEach((a, i) => { out[a.name] = i < n ? a.md5 : null; });
  return out;
};
const HIDE_LIST = [ONE, "com.amazon.device.smarthome.wifid", "com.amazon.avatara"];
// Read out of the file rather than restated, for the same reason the step list
// is: a window that gains a step upstream must fail here, not here-and-silently.
const WINDOW = JSON.parse(
  liftList("_RESUME_WINDOW")
    .slice(liftList("_RESUME_WINDOW").indexOf("["), liftList("_RESUME_WINDOW").indexOf("];") + 1)
    .replaceAll("'", '"'));
if (!WINDOW.includes("patch_boot") || !WINDOW.includes("reboot")) {
  throw new Error(`_RESUME_WINDOW no longer names the FireOS recovery steps: ${WINDOW}`);
}

// A device with every piece of evidence the FireOS flow can read.
function evidence(over = {}) {
  return {
    mode: "android",
    steps: FIREOS,
    stepMode: FIREOS_MODE,
    window: WINDOW,
    debloat: HIDE_LIST,
    oww: { assets: ASSETS, found: foundAll() },
    probes: {
      props: props("permissive"),
      root: root(true),
      services: services(true),
      pm_ready: pm_ok,
      packages: "",
      wpa_status: wpa("COMPLETED"),
      server: server("server_a", "yes"),
      ...over,
    },
  };
}
const run = (over, current = 0) =>
  resumeStepFrom({ ...evidence(over), current });

let fails = 0;
function check(what, cond, detail) {
  if (cond) console.log(`ok    ${what}`);
  else { console.log(`FAIL  ${what}${detail ? `\n        ${detail}` : ""}`); fails++; }
}

// ── the fully-provisioned device ─────────────────────────────────────────────
//
// Nothing to resume at, and it says so rather than pointing at a step. This is
// the shape the reconnect lands on most often: the pointer was left in the
// recovery window, the device finished the whole flow while nobody was watching,
// and every step's evidence is on it.
{
  const r = run();
  check("a fully-provisioned device has no step to resume at",
        r.index === -1 && r.stepId === null && r.moved === false,
        JSON.stringify({ index: r.index, moved: r.moved }));
  check("...and the report says so", /nothing to resume at/.test(r.reason), r.reason);
}

// ── the issue's own example ─────────────────────────────────────────────────
//
// "This device already has root and a patched boot image; resuming at Disable
// Alexa is a better answer than a pointer that survived a reboot the device did
// not." WiFi is the first step with nothing on it.
{
  const r = run({ wpa_status: wpa("SCANNING") }, 4);
  check("the pointer left in the recovery window moves forward past it",
        r.moved && r.index === 10 && r.stepId === "wifi", JSON.stringify(r));
  check("...and names the step it lands on",
        /resuming at step 11\/13, Configure WiFi/.test(r.reason), r.reason);
  check("...and reports the recovery window as done",
        r.findings.slice(1, 6).every(f => f.state === "done"),
        JSON.stringify(r.findings.slice(1, 6).map(f => f.state)));
}

// Every reading is reported, whatever it says, and the report precedes the move.
{
  const r = run({ wpa_status: wpa("SCANNING") }, 4);
  const texts = r.readings.map(g => g.text).join(" | ");
  check("the report names the cmdline, root and the init service",
        /SELinux-permissive/.test(texts) && /uid=0/.test(texts)
        && /init has the EchoMuse service/.test(texts), texts);
  check("the report names both package lists",
        /none of the 11 Alexa packages/.test(texts)
        && /none of the 3 debloat packages/.test(texts), texts);
  check("the report names the WiFi, the binary and the assets",
        /wpa_state=COMPLETED|still scanning/.test(texts)
        && /links to server_a/.test(texts)
        && /4 of 4 wake word assets match by md5/.test(texts), texts);
  check("every reading carries one of the three states",
        r.readings.every(g => ["done", "not done", "unknown"].includes(g.state)),
        JSON.stringify(r.readings.map(g => g.state)));
}

// ── presence is not proof ───────────────────────────────────────────────────
//
// The interrupted asset push. Three of the four files are on the device and read
// as installed; the step that verifies them md5s every one, and a device with
// three of four fails at dlopen naming nothing.
{
  const r = resumeStepFrom({ ...evidence(),
    oww: { assets: ASSETS, found: foundN(3) }, current: 10 });
  check("three of four wake word assets is not an installed set",
        r.moved && r.index === 12 && r.stepId === "install_oww", JSON.stringify(r));
  check("...and the report says how many matched",
        /3 of 4 wake word assets match by md5/.test(r.readings.map(g => g.text).join(" ")));
}
{
  // All four present, one of them truncated. Presence is identical to the case
  // above and the answer must be too.
  const found = foundAll();
  found["alexa.tflite"] = "d".repeat(31) + "e";
  const r = resumeStepFrom({ ...evidence(),
    oww: { assets: ASSETS, found }, current: 10 });
  check("a present file whose md5 differs is not an installed file",
        r.moved && r.index === 12, JSON.stringify(r));
}

// ── an unreadable probe is not a pass ───────────────────────────────────────
{
  // Every probe answered, and all four say nothing useful.
  const r = resumeStepFrom({ steps: FIREOS, stepMode: FIREOS_MODE, mode: "android",
    window: WINDOW, debloat: HIDE_LIST, oww: { assets: ASSETS, found: foundAll() },
    probes: { props: "", root: "", services: "", pm_ready: "", packages: "",
              wpa_status: "", server: "" }, current: 4 });
  // Only the two connection steps, which the live handle proves, and the assets,
  // which were read from the manifest rather than from a probe. Nothing a probe
  // was asked for counts as proven.
  check("no probe reading means no probed step is proven",
        r.findings.filter(f => f.state === "done").map(f => f.index).join(",") === "0,6,12",
        JSON.stringify(r.findings.map(f => f.state)));
  check("...and it stops at the recovery window rather than past it",
        r.index === 1 && r.stepId === "connect_twrp", JSON.stringify(r));
  check("...and refuses to move, because that window runs in TWRP",
        !r.moved && /runs in TWRP recovery/.test(r.reason), r.reason);
}
{
  // `su` answering nothing is indistinguishable from the symlink being gone,
  // which is the `_CLEARCHK` reason. It must read as unknown, not as absent.
  const r = run({ server: "" }, 10);
  check("a server probe with no sentinel is unknown rather than absent",
        r.readings[6].state === "unknown", JSON.stringify(r.readings[6]));
  check("...so the pointer lands ON Install EchoMuse instead of skipping it",
        r.moved && r.index === 11 && r.stepId === "install_em", JSON.stringify(r));
}
{
  // A supplicant that did not answer is not a device that is not on a network.
  const r = run({ wpa_status: "Failed to connect to non-global ctrl_ifname" }, 8);
  check("an unreachable wpa_cli is unknown, not 'not connected'",
        r.readings[5].state === "unknown", JSON.stringify(r.readings[5]));
  check("...and Configure WiFi is where it resumes", r.moved && r.index === 10,
        JSON.stringify(r));
}
{
  // pm refusing must not read as an empty package list. This is #91's
  // distinction one level up: "this build lacks these packages" and "the
  // package manager is broken" are the same output from a grep.
  for (const refuse of ["Could not access the Package Manager",
                        "java.lang.NullPointerException: ... on a null object reference"]) {
    const r = run({ pm_ready: refuse, packages: "" }, 8);
    check(`pm refusing ("${refuse.slice(0, 24)}…") is not an empty package list`,
          r.readings[3].state === "unknown" && r.readings[4].state === "unknown",
          JSON.stringify([r.readings[3], r.readings[4]]));
    check("...and neither package step is taken as done",
          r.findings[8].state === "unknown" && r.findings[9].state === "unknown",
          JSON.stringify([r.findings[8].state, r.findings[9].state]));
  }
}
{
  // The debloat list is the controller's, and a failed fetch leaves its step
  // unproven rather than empty.
  const r = resumeStepFrom({ ...evidence({ wpa_status: wpa("SCANNING") }),
    debloat: null, current: 8 });
  check("a debloat list that could not be fetched is not an empty one",
        r.readings[4].state === "unknown" && r.findings[9].state === "unknown",
        JSON.stringify(r.findings[9]));
  check("...and the report says it could not read it",
        /could not read which debloat packages/.test(r.readings[4].text), r.readings[4].text);
}
{
  // No manifest at all — an old controller, or a fetch that failed.
  const r = resumeStepFrom({ ...evidence(), oww: null, current: 11 });
  check("no wake word manifest is unknown, never an installed set",
        r.findings[12].state === "unknown" && r.index === 12 && r.moved, JSON.stringify(r));
}

// ── never skip a step that is not proven ────────────────────────────────────
//
// Debloat gone but Disable Alexa not: re-running Disable Alexa is free and
// skipping Debloat is not, so the resume point is the FIRST such step.
{
  const r = run({ packages: PKGS(ONE) }, 7);
  check("the first unproven step wins, not the last proven one",
        r.moved && r.index === 8 && r.stepId === "disable_alexa", JSON.stringify(r));
  check("...even though everything after it is proven",
        r.findings.slice(10).every(f => f.state === "done"),
        JSON.stringify(r.findings.slice(8).map(f => f.state)));
  check("...and the report counts what is still visible",
        /1 of 11 Alexa packages are still visible/.test(r.readings[3].text), r.readings[3].text);
}
{
  // A package that is not installed counts as handled — that is exactly what
  // runDisableAlexa treats `Unknown package` as, and it is why #91 could be
  // finished at all. The one amazon package the grep returns here is not in
  // either list.
  const r = run({ packages: PKGS("com.amazon.something.unrelated") }, 7);
  check("a package not on either list does not block either step",
        r.findings[8].state === "done" && r.findings[9].state === "done"
        && r.index === -1, JSON.stringify(r));
}

// ── never backwards, and never automatic redo ────────────────────────────────
{
  // The pointer is at Install EchoMuse and the evidence says Disable Alexa has
  // lost its packages — /data wiped, or the device re-provisioned by hand. This
  // is the case the issue says not to automate: re-running steps 8 and 9 is how
  // work gets destroyed, and step 2 writes a boot partition.
  const r = run({ packages: PKGS(ONE) }, 11);
  check("a step the pointer has passed is never re-opened",
        !r.moved && r.index === 8, JSON.stringify(r));
  check("...and the report says why it was left alone",
        /already on step 12/.test(r.reason) && /needs doing again/.test(r.reason), r.reason);
  check("...while still reporting the missing evidence",
        /1 of 11 Alexa packages are still visible/.test(r.readings[3].text), r.readings[3].text);
}
{
  const r = run({ wpa_status: wpa("SCANNING") }, 10);
  check("being already on the unproven step moves nothing",
        !r.moved && /already on Configure WiFi/.test(r.reason), r.reason);
}

// ── the mode guard ──────────────────────────────────────────────────────────
//
// Never resume to a step whose required mode the device is not in. In Android
// /dev/block/other-boot is amonet's unlock payload rather than a kernel, so a
// TWRP step run there writes over the unlock — classifyBootTarget refuses that
// write, and this keeps the wizard from walking anybody toward it.
{
  const r = run({ props: props("enforce"), services: services(false) }, 2);
  check("a stock device in Android does not resume into the TWRP window",
        !r.moved && r.index === 1 && r.stepId === "connect_twrp", JSON.stringify(r));
  check("...and the report names the mode it needs",
        /runs in TWRP recovery, but the device is in Android/.test(r.reason), r.reason);
}
{
  // An unreadable banner is not evidence of a mode either.
  const r = resumeStepFrom({ ...evidence({ wpa_status: wpa("SCANNING") }),
    mode: "unknown", current: 4 });
  check("an unreadable banner moves nothing",
        !r.moved && r.reason.includes("could not be read from its banner"), r.reason);
}
{
  // The same evidence, the wizard in recovery: now the window IS reachable, and
  // a stock device resumes at its first step rather than at Patch Boot Image.
  const r = resumeStepFrom({ ...evidence({ props: props("enforce"),
    services: services(false), packages: PKGS(ONE) }), mode: "twrp", current: 0 });
  check("a TWRP device resumes into the TWRP window",
        r.moved && r.index === 1 && r.stepId === "connect_twrp", JSON.stringify(r));
  check("...and nothing past the window is claimed",
        r.findings[2].state === "not done", JSON.stringify(r.findings[2]));
}

// ── the recovery window needs all three readings ────────────────────────────
//
// Root alone is not enough to skip Patch Boot Image: a device rooted by hand
// from docs/rooting.md runs root too, and its ramdisk has never heard of
// start_server.sh. Resuming past it on that device provisions a Dot that
// appears in the dashboard and never answers.
for (const [over, missing] of [
  [{ services: services(false) }, "init has NO EchoMuse service"],
  [{ props: props("enforce") }, "boot image cmdline is NOT SELinux-permissive"],
  [{ root: root(false) }, "no root yet"],
]) {
  const r = run(over, 4);
  check(`without "${missing}" the window is not done`,
        !r.moved && r.index === 1, JSON.stringify({ index: r.index, moved: r.moved }));
  check(`...and the report names it`, r.readings.some(g => g.text.includes(missing)),
        JSON.stringify(r.readings.slice(0, 3).map(g => g.text)));
}
{
  // Root and the cmdline, but the init entries missing. The reading that says so
  // has to be the one that stops the move, and it is the least obvious of the
  // three, so pin it on its own.
  const r = run({ services: services(false) }, 4);
  check("root plus a permissive cmdline does not prove the init.rc entries",
        r.findings[1].state === "not done" && r.findings[5].state === "not done",
        JSON.stringify(r.findings.slice(1, 6).map(f => f.state)));
}

// ── the emOS flow is never repositioned on this evidence ────────────────────
//
// Its recovery window is unreadable by construction — there is no `su` in a
// recovery ramdisk, no /data view of what the escrow did, and emOS has no init
// service table to read `init.svc.echomuse` out of — so `windowDone` is false
// and the first unproven step is inside that window. Steps 7 and 8 run on the
// serial console, which `_bannerMode` cannot report at all, so a resume into
// either is a move the mode guard has to refuse rather than a coincidence.
//
// The invariant is that NOTHING here moves the emOS flow past Connect to TWRP.
// The wiring does not call it for emOS at all; these cases are what a future
// caller would get if it did.
for (const mode of ["android", "twrp", "unknown"]) {
  for (const current of [0, 4, 8]) {
    const r = resumeStepFrom({ steps: EMOS, stepMode: EMOS_MODE, mode,
      debloat: HIDE_LIST, oww: { assets: ASSETS, found: foundAll() },
      probes: evidence().probes, current });
    if (r.moved && r.index >= 2) {
      check(`emos ${mode}@${current} must not resume past Connect to TWRP`, false,
            JSON.stringify({ index: r.index, moved: r.moved, reason: r.reason }));
    }
    if (r.moved && r.index >= 7) {
      check(`emos ${mode}@${current} must never resume onto a serial step`, false,
            JSON.stringify({ index: r.index, moved: r.moved }));
    }
    // Whatever it reports, the escrow and the partition write are never called
    // done on Android-side readings — the one thing that must not happen.
    if (mode === "twrp" && (r.findings[2].state === "done" || r.findings[6].state === "done")) {
      check(`emos ${mode}@${current} must not infer its own window from inside it`, false,
            JSON.stringify(r.findings.map(f => f.state)));
    }
  }
}
check("the emOS flow is never repositioned past its TWRP window, in any mode", true);
{
  // In Android, with evidence that proves nothing about recovery. It stops at
  // Connect to TWRP and says why, rather than guessing about the escrow.
  const r = resumeStepFrom({ steps: EMOS, stepMode: EMOS_MODE, mode: "android",
    debloat: HIDE_LIST, oww: { assets: ASSETS, found: foundAll() },
    probes: evidence().probes, current: 4 });
  check("emos in Android stops at Connect to TWRP",
        !r.moved && r.index === 1 && r.stepId === "connect_twrp", JSON.stringify(r));
  check("...and the escrow is not claimed",
        r.findings[2].state !== "done", JSON.stringify(r.findings[2]));
}

// ── the install probe's three answers ───────────────────────────────────────
//
// The one probe added for this, and it has to separate three things: su ran and
// the install is there, su ran and it is not, and su did not run at all.
{
  const r = run({ server: server("server_b", "yes") }, 11);
  check("a symlink pointing at the other slot is not an install",
        r.findings[11].state === "not done" && r.index === 11, JSON.stringify(r.findings[11]));
}
{
  const r = run({ server: server("", "no") }, 11);
  check("a missing binary and a missing symlink are not an install",
        r.findings[11].state === "not done" && r.index === 11, JSON.stringify(r.findings[11]));
}
{
  const r = run({ server: `_SERVERCHK\r\nserver_a\r\n_SERVERA=yes\r\n` }, 4);
  check("CRLF line endings parse (this is Android's shell)",
        r.findings[11].state === "done", JSON.stringify(r.findings[11]));
}
{
  const r = run({ server: "_SERVERA=yes" }, 11);
  check("a server probe missing its sentinel is unknown",
        r.findings[11].state === "unknown", JSON.stringify(r.findings[11]));
}

// ── nothing in the decision is index-arithmetic across the two flows ────────
{
  // install_em is 11 in the FireOS flow and 3 in emOS. A function that held an
  // index would answer "3" for both.
  const r = run({ server: server("", "no") }, 4);
  check("the resume step is named by ID, not by index",
        r.stepId === "install_em" && FIREOS[11].id === "install_em",
        JSON.stringify({ stepId: r.stepId, index: r.index }));
  const e = resumeStepFrom({ steps: EMOS, stepMode: EMOS_MODE, mode: "twrp",
    debloat: HIDE_LIST, oww: { assets: ASSETS, found: foundAll() },
    probes: evidence().probes, current: 0 });
  check("...and the same flow answers with its own step 2",
        e.stepId === "connect_twrp" && EMOS[1].id === "connect_twrp"
        && FIREOS[1].id === "connect_twrp",
        JSON.stringify({ stepId: e.stepId, index: e.index }));
}

// ── every step is accounted for ─────────────────────────────────────────────
{
  const r = run();
  check("there is one finding per step, in step order",
        r.findings.length === FIREOS.length
        && r.findings.every((f, i) => f.id === FIREOS[i].id && f.index === i),
        JSON.stringify(r.findings.map(f => f.id)));
  check("findings carry a label, so the report can name the step",
        r.findings.every(f => typeof f.label === "string" && f.label.length > 0));
  check("a missing probes object is treated as unreadable, not as an error",
        resumeStepFrom({ steps: FIREOS, stepMode: FIREOS_MODE, mode: "android" }).moved === false);
}

// ── the wiring ──────────────────────────────────────────────────────────────
//
// Comments stripped first, for the reason boot_target.test.mjs gives: a guard
// that greps for the thing it forbids otherwise finds the comment explaining
// why it is forbidden and passes.
{
  const strip = (s) => s.replace(/^[ \t]*\/\/.*$/gm, "");
  const resume = strip(liftFunction("resumeFromDeviceState"));
  const recon  = strip(liftFunction("reconnectAdb"));
  check("reconnectAdb runs the inference after the mode mismatch is reported",
        /resumeFromDeviceState\(c, got\)/.test(recon), recon.slice(-400));
  check("...and only for the FireOS flow, whose steps carry the evidence",
        /if \(!isEmos\) await resumeFromDeviceState/.test(recon));
  const verdictAt = resume.indexOf("r.reason");
  const moveAt    = resume.indexOf("setStep(r.index)");
  check("the report is emitted before the pointer moves",
        verdictAt > 0 && moveAt > verdictAt, `verdict@${verdictAt} move@${moveAt}`);
  check("...and every reading is emitted before it too",
        resume.indexOf("for (const g of r.readings)") < verdictAt);
  check("moving requires the inference to have moved it",
        /if \(r\.moved\) \{/.test(resume));
  check("an unreadable probe is stored as null rather than as an error string",
        /probes\[name\] = null;/.test(resume));
  // The glyph table is keyed on the three STATE NAMES, so the log's own
  // severity test has to use those names too. Compared against the literal
  // `false` it silently never fires, which is a report that prints ✓ for a
  // device with nothing installed.
  check("the report keys its severity on the state NAME, not on a boolean",
        /g\.state === _RESUME_DONE \? undefined : 'warn'/.test(resume),
        resume.slice(resume.indexOf("_RESUME_MARK[g.state]"), 220));
  check("the glyph table is keyed on the same three names the function emits",
        src.includes("const _RESUME_MARK = { done: '✓', 'not done': '✗', unknown: '?' }"));
  check("nothing here reaches the boot-partition write path",
        !/classifyBootTarget|boot\.target|other-boot|dd if=/.test(resume));
  check("no step's own behaviour was changed — the inference only writes state",
        /markStep\(i, 'done'\)/.test(resume) && !/runStep|runPatchBoot|runInstall/.test(resume));
}

// The probe list reuses the diagnostics commands by reference rather than
// growing a second copy of each one. A probe string typed twice is a command
// that changes on one side of a wire nobody watches.
{
  const refs = src.slice(src.indexOf("const _RESUME_PROBES"));
  const block = refs.slice(0, refs.indexOf("\n  };"));
  const names = [...block.matchAll(/^\s*(\w+):\s*_PROVISION_PROBES\.(\w+),?$/gm)];
  check("the resume probes reuse the diagnostics commands by reference",
        names.length >= 6 && names.every(m => m[1] === m[2]),
        block.slice(0, 300));
  check("...and exactly one new command is added",
        (block.match(/server:\s*'su -c/g) || []).length === 1);
}

// The window is the FireOS flow's declaration, and it is the only thing that
// lets a reading taken in Android mark a TWRP step done. If it ever names a step
// the FireOS flow does not have, that step is silently unprovable; if it ever
// names one it does not, the inference claims work nobody did — which for
// `flash_emos`/`build_emos` is a skipped partition write.
{
  check("the window names steps of the FireOS flow and nothing else",
        WINDOW.every(id => FIREOS.some(s => s.id === id)),
        JSON.stringify(WINDOW));
  check("...and it is exactly the FireOS flow's TWRP steps",
        JSON.stringify(WINDOW) === JSON.stringify(
          FIREOS.filter((_, i) => FIREOS_MODE[i] === "twrp").map(s => s.id)),
        JSON.stringify({ window: WINDOW, twrp: FIREOS.filter((_, i) => FIREOS_MODE[i] === "twrp").map(s => s.id) }));
  check("the wiring passes it, so the FireOS window is the one that applies",
        /window: _RESUME_WINDOW/.test(liftFunction("resumeFromDeviceState")));
  check("...and the emOS flow is passed no window at all",
        /if \(!isEmos\) await resumeFromDeviceState/.test(liftFunction("reconnectAdb")));
}

console.log(fails ? "\nFAILED" : "\nall ok");
process.exit(fails ? 1 : 0);