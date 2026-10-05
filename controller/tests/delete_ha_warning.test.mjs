// Tests for _deleteHaWarning() in dashboard.jsx — what the delete
// confirmation says about Home Assistant (#375).
//
//     node controller/tests/delete_ha_warning.test.mjs
//
// Source extraction rather than import, for the reason wipe_verdict gives:
// the dashboard compiles to a single classic script with no module boundary,
// so the alternative is a second copy that drifts.
//
// Why the warning exists: a device's ESPHome identity is derived from its
// serial (the MAC is md5(device_id), the mDNS name is echomuse-<last 12>),
// so a device deleted and re-approved advertises the same name and the same
// MAC. Its port is not derived — the counter only moves forwards, and a
// deleted device's satellite is dropped — so HA keeps an entry keyed on the
// old port, discovery offers nothing new, and the entry dials a dead port
// until it is deleted by hand. Reproduced on 2.22.0-ea.4, 2026-08-28; the
// ten minutes that produced the issue were spent finding that by hand.

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

const { _deleteHaWarning } = await import(
  "data:text/javascript;base64," + Buffer.from(
    liftArrow("_deleteHaWarning") + "\nexport { _deleteHaWarning };"
  ).toString("base64"));

let failures = 0;
function check(name, cond, detail) {
  if (cond) return;
  failures++;
  console.error(`FAIL: ${name}${detail ? `\n      ${detail}` : ""}`);
}

// Nothing derived from a missing field may reach the reader. "port undefined"
// and "port null" are worse than no warning at all: they make the one screen
// that says "delete the entry by hand" look broken instead.
const NEVER = ["undefined", "null", "NaN", "[object Object]", "${"];

const named = _deleteHaWarning({ label: "Kitchen", esphome_port: 16001 });
check("a device with a port has that port named",
      named.includes("16001"), named);
check("the device is named, so the operator knows which entry to look for",
      named.includes("Kitchen"), named);
check("the port reads as a port", /on port 16001/.test(named), named);
for (const bad of NEVER) {
  check(`a labelled device does not render ${bad}`, !named.includes(bad), named);
}

// The warning's whole content is the instruction, so both halves of it are
// load-bearing: what to do, and when.
check("it says where the entry lives", /Home Assistant/.test(named), named);
check("it says to delete the entry before adding the device back",
      /delete it there before adding/i.test(named), named);

// ── No port on record ──
// A device that never had a satellite: the row is pending, or the port was
// never allocated. There is no number to name, and the sentence must not
// invent one — but it must still say the true thing.
for (const [what, device, named] of [
  ["a NULL port", { label: "Kitchen", esphome_port: null }, "Kitchen"],
  ["no port key at all", { label: "Kitchen" }, "Kitchen"],
  ["a device object that is undefined", undefined, "this device"],
]) {
  const w = _deleteHaWarning(device);
  check(`${what} renders no port at all`, !/port\s*[:(]/.test(w), w);
  for (const bad of NEVER) {
    check(`${what} does not render ${bad}`, !w.includes(bad), w);
  }
  check(`${what} still names the device`, w.includes(named), w);
  check(`${what} still says to delete the entry first`,
        /delete it there before adding/i.test(w), w);
}

// Absence stores as NULL, never 0 — so a 0 is a value the dashboard must show
// rather than swallow. Ports start at 16001, which is why `!= null` and not a
// truthiness test: reading 0 as "no port" would be the conflation the project
// rules exist to prevent.
check("a port of 0 is reported as 0, not treated as absent",
      /on port 0\b/.test(_deleteHaWarning({ label: "Kitchen", esphome_port: 0 })),
      _deleteHaWarning({ label: "Kitchen", esphome_port: 0 }));

// ── No label ──
// A pending device has no label until it is approved, and the header shows its
// serial in that state. The warning falls back to the serial for the same
// reason, so it reads as a name rather than as a hole in a sentence.
const unlabelled = _deleteHaWarning({ device_id: "G090LF11803611NF", esphome_port: 16101 });
check("an unlabelled device is named by its serial",
      unlabelled.includes("G090LF11803611NF"), unlabelled);
// Nothing that reads as a hole: no doubled space where the name would be, no
// empty quotes, and the sentence still closes.
check("an unlabelled device does not read as broken",
      !/\s{2,}/.test(unlabelled)
      && !unlabelled.includes("''")
      && unlabelled.startsWith("Home Assistant keeps its entry for ")
      && unlabelled.endsWith(" back."), unlabelled);

// UI copy is a clause or two. The first version restated the whole mechanism
// and told the reader why Home Assistant keys what it keys on, which is a fact
// about HA rather than something the operator can act on.
check("the banner is short enough to read before confirming",
      named.split(/[.!?]/).filter(Boolean).length <= 2, named);
check("it does not explain HA's keying to the operator",
      !/keys/i.test(named), named);
check("a device with neither label nor id still reads as a sentence",
      _deleteHaWarning({}).includes("this device"), _deleteHaWarning({}));

// ── It reaches both delete paths ──
// The Detail modal's confirmation, and the wizard's duplicate-serial delete —
// which had no confirmation at all. A warning that is lifted and tested but
// never rendered saves nobody.
const modal = src.slice(src.indexOf("{isAdmin && confirmDelete && ("),
                        src.indexOf("{device.approved ? ("));
check("the Detail confirmation renders the warning", modal.includes("_deleteHaWarning(device)"), modal);
check("and the whole warning, not a fragment",
      modal.includes("<span>{_deleteHaWarning(device)}</span>"), modal);

const wizardHit = (src.match(/_deleteHaWarning\(dup \|\| \{ device_id: duplicateDeviceId \}\)/g) || []).length;
check("the wizard's duplicate-serial delete renders it too", wizardHit === 1,
      `found ${wizardHit} call sites`);
// The wizard resolves the matched device out of knownDevices rather than
// carrying it on the error, so this path names the port as well — and falls
// back to the serial alone if the list no longer has it.
check("the wizard resolves the matched device so it can name the port",
      /const dup = \(knownDevices \|\| \[\]\)\.find\(d => d && d\.device_id === duplicateDeviceId\)/.test(src));
check("and falls back to the serial when the device is gone from the list",
      src.includes("dup || { device_id: duplicateDeviceId }"));
check("the wizard warning sits beside the delete it warns about",
      src.indexOf("_deleteHaWarning(dup || { device_id: duplicateDeviceId })")
      > src.indexOf("{stepFailed && !INPUT_STEPS.has(cur.id) && (")
  && src.indexOf("_deleteHaWarning(dup || { device_id: duplicateDeviceId })")
     < src.indexOf('>Delete "{duplicateDeviceId}" from controller</Pill>'));

// The wizard's keep-its-record button states the inverse, and it is right:
// keeping the row keeps the port, so HA keeps its satellite. Nothing here may
// contradict that. Matched across line breaks — the sentence is wrapped in a
// JSX comment, which a single-line regex silently fails on.
const flat = src.replace(/\s+/g, " ");
check("keeping the record still promises the port is kept",
      /the device keeps its port, so Home Assistant keeps its satellite/.test(flat));

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("delete_ha_warning: all checks passed");