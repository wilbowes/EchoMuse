// The emOS flow's step numbers, against the switch that runs them.
//
//     node controller/tests/emos_steps.test.mjs
//
// A step with no `case` in runStep is marked done having done nothing, so a
// renumbering that misses one fails silently — which is what happened when the
// wake word asset step was appended after FireOS's last step and never fired
// (2026-07-31), and again when Connect Device was dropped from the emOS flow
// (#454). Every table that keys on a step number is checked against the step
// list itself, so the next renumbering cannot miss one either.
//
// Source extraction rather than import: the dashboard compiles to one classic
// script with no module boundary.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

let failures = 0;
function check(label, ok, detail) {
  if (!ok) {
    failures++;
    console.error(`FAIL  ${label}${detail ? `\n      ${detail}` : ""}`);
  } else {
    console.log(`ok    ${label}`);
  }
}

function stepIds(table) {
  const at = src.indexOf(`const ${table} = [`);
  if (at < 0) throw new Error(`dashboard.jsx no longer defines ${table}`);
  const block = src.slice(at, src.indexOf("\n];", at));
  return [...block.matchAll(/\{ id: '([a-z_]+)'/g)].map(m => m[1]);
}

function emosSwitch() {
  const from = src.indexOf("if (isEmos) switch (stepIdx)");
  if (from < 0) throw new Error("runStep's isEmos switch is gone");
  const block = src.slice(from, src.indexOf("else switch (stepIdx)", from));
  const parts = block.split(/^\s*case (\d+):/m);
  const order = [];
  const cases = {};
  for (let i = 1; i < parts.length; i += 2) {
    order.push(Number(parts[i]));
    cases[Number(parts[i])] = parts[i + 1];
  }
  return { order, cases };
}

const ids = stepIds("_EMOS_STEPS");
const last = ids.length - 1;
const { order, cases } = emosSwitch();

// The runner each step must reach. Pinned by ID, so a case left one step
// behind fails on the step it now runs rather than on a count.
const RUNNER = {
  connect_twrp: "runConnectTwrp(",
  escrow_boot: "runEscrowBoot(",
  install_em: "runInstallEchoMuse(",
  install_oww: "runInstallOwwAssets(",
  build_emos: "runBuildEmos(",
  flash_emos: "runFlashEmos(",
  reboot_watch: "runRebootAndWatch(",
  wifi_register: "runEmosWifi(",
};

check("the emOS flow starts in TWRP", ids[0] === "connect_twrp", ids[0]);
check("Connect Device is gone from it", !ids.includes("connect_android"));
check("and FireOS still has it", stepIds("_WIZARD_STEPS").includes("connect_android"));
check("the last step is still Configure WiFi", ids[last] === "wifi_register", ids[last]);
check("eight steps, not nine", ids.length === 8, `${ids.length}: ${ids}`);
check("every step has a runner pinned", ids.every(id => RUNNER[id]), ids.join(", "));

check("the switch has no case past the last step",
  Math.max(...Object.keys(cases).map(Number)) === last,
  `cases ${Object.keys(cases).join(",")} vs ${last}`);
check("and no step without a case",
  Object.keys(cases).length === ids.length,
  `${Object.keys(cases).length} cases for ${ids.length} steps`);
check("each case appears once, in step order",
  order.join() === ids.map((_, i) => i).join(), order.join());
for (const [i, id] of ids.entries()) {
  const body = cases[i] || "";
  check(`step ${i} (${id}) runs ${RUNNER[id]}`, body.includes(RUNNER[id]),
    body.trim().split("\n")[0]);
}

// The identity gate is what Connect Device existed for, and it is now step 0's
// second half: without it the duplicate check and the board never happen.
check("step 0 identifies the device in TWRP",
  cases[0].includes("runConnectDevice(") && cases[0].includes("runConnectTwrp("));
check("step 0 still offers the data wipe", cases[0].includes("runWipeData("));
check("nothing in the emOS flow reaches runConnectAndroid",
  !Object.values(cases).some(b => b.includes("runConnectAndroid(")));

// The step that broke before: it must be a case AND auto-run, or a mandatory
// push waits on a button nobody presses. The flash must NOT auto-run.
const autoAt = src.indexOf("const autoSteps =");
const autoLine = src.slice(autoAt, src.indexOf("\n", autoAt));
const sets = [...autoLine.matchAll(/new Set\(\[([^\]]*)\]\)/g)];
const emosAuto = sets.length === 2
  ? new Set([...sets[0][1].matchAll(/\d+/g)].map(m => Number(m[0])))
  : new Set([-1]);
check("one autoSteps set per flow", sets.length === 2, autoLine);
for (const id of ["escrow_boot", "install_oww", "build_emos"]) {
  check(`${id} auto-runs`, emosAuto.has(ids.indexOf(id)),
    `auto ${[...emosAuto].join(",")}`);
}
check("the flash does not auto-run", !emosAuto.has(ids.indexOf("flash_emos")));
check("every auto step exists", [...emosAuto].every(n => n >= 0 && n <= last));

// The 2026-07-31 failure: a step that ends by rebooting the device away closes
// the ADB connection, so everything after it is unreachable. The asset push
// must stay in recovery in the emOS flow instead.
const owwAt = src.indexOf("async function runInstallOwwAssets");
const oww = src.slice(owwAt, src.indexOf("\n  async function ", owwAt + 1));
check("the wake word push does not reboot the emOS device away",
  oww.indexOf("if (isEmos)") > 0
  && oww.indexOf("if (isEmos)") < oww.indexOf("su -c reboot"),
  "the isEmos guard must come before the reboot");

// The build's source is the release, so the auto path has to pass useLatest.
check("the auto-run build passes useLatest",
  new RegExp(`runStep\\(step, isEmos && step === ${ids.indexOf("build_emos")}\\)`).test(src));

// Mode, connect and serial tables.
const modeBlock = src.slice(src.indexOf("const _EMOS_STEP_MODE = {"),
                            src.indexOf("};", src.indexOf("const _EMOS_STEP_MODE = {")));
const modes = Object.fromEntries(
  [...modeBlock.matchAll(/(\d+):\s*'(\w+)'/g)].map(m => [Number(m[1]), m[2]]));
const serialSet = src.slice(src.indexOf("const _EMOS_SERIAL_STEPS = new Set(["));
const serial = new Set([...serialSet.slice(0, serialSet.indexOf("])")).matchAll(/\d+/g)]
  .map(m => Number(m[0])));
const connectBlock = src.slice(src.indexOf("const _EMOS_CONNECT_STEPS = new Set(["));
const connect = new Set([...connectBlock.slice(0, connectBlock.indexOf("])")).matchAll(/\d+/g)]
  .map(m => Number(m[0])));

check("every step has a mode", Object.keys(modes).length === ids.length,
  Object.keys(modes).join(","));
check("no step runs in Android", !Object.values(modes).includes("android"),
  Object.values(modes).join(","));
check("every TWRP step before the console is 'twrp'",
  ids.every((_, i) => modes[i] === (serial.has(i) ? "serial" : "twrp")),
  JSON.stringify(modes));
check("the serial steps are exactly the ones marked serial",
  [...serial].sort().join() === Object.keys(modes).filter(k => modes[k] === "serial").join(),
  `${[...serial].sort().join()} vs ${Object.keys(modes).filter(k => modes[k] === "serial").join()}`);
check("the serial steps are reboot_watch and wifi_register",
  serial.has(ids.indexOf("reboot_watch")) && serial.has(ids.indexOf("wifi_register")));
check("the flow connects once, at step 0",
  connect.size === 1 && connect.has(0), [...connect].join(","));

// The panels keyed on a step number. Their set is pinned here because an
// unshifted panel shows up on a step that does something else, and a stale 8
// is a panel nobody can reach.
const panels = new Set([...src.matchAll(/\{isEmos && step === (\d+) && stepState\[(\d+)\]/g)]
  .map(m => `${m[1]}`));
check("the panel indices and the indices they read agree",
  [...src.matchAll(/\{isEmos && step === (\d+) && stepState\[(\d+)\]/g)]
    .every(m => m[1] === m[2]));
check("the step panels land on install/build/flash/reboot/wifi",
  [...panels].sort().join() ===
    [ids.indexOf("install_em"), ids.indexOf("build_emos"), ids.indexOf("flash_emos"),
     ids.indexOf("reboot_watch"), ids.indexOf("wifi_register")].sort().join(),
  [...panels].join(","));
const flash = ids.indexOf("flash_emos");
const reboot = ids.indexOf("reboot_watch");
check("the restore is offered on a failed flash and a failed first boot",
  src.includes(`(isEmos ? (step === ${flash} || step === ${reboot}) : (step >= 2 && step <= 4))`));

if (failures) { console.error(`\n${failures} failed`); process.exit(1); }
console.log("\nall passed");
