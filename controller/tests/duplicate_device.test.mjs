// Tests for duplicateVerdict() in dashboard.jsx — what the provisioning
// wizard does at step 0 about a serial the controller may already know.
//
//     node controller/tests/duplicate_device.test.mjs
//
// Source extraction rather than import, for the reason the other dashboard
// tests give: the dashboard compiles to a single classic script with no
// module boundary, so the alternative is a second copy that drifts.
//
// The case that matters is 'keep': re-running the wizard is the only way to
// move an existing device to a newer emOS until #573, and deleting its record
// reassigns its ESPHome port — ports are never reused — so every satellite has
// to be re-added in Home Assistant. Keeping the row is safe server-side,
// because ensure_device_token returns the stored token and leaves approval
// alone.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

function liftFunction(name) {
  const start = src.indexOf(`function ${name}`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}()`);
  let depth = 0, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) break; }
  }
  return src.slice(start, i + 1);
}

const duplicateVerdict = eval(`(${liftFunction("duplicateVerdict")})`);

let fails = 0;
function check(what, cond) {
  if (!cond) { console.error(`FAIL ${what}`); fails++; }
  else console.log(`ok   ${what}`);
}

const SERIAL = "G090LF11803611NF";
const live    = { device_id: SERIAL, label: "Kitchen", firmware_ver: "v2.16.0" };
const neverUp = { device_id: SERIAL, label: null, firmware_ver: null };
const other   = { device_id: "G090LF1180440EFF", firmware_ver: "v2.16.0" };

check("an unknown serial proceeds",
  duplicateVerdict([other], SERIAL, null).action === "proceed");

check("no devices at all proceeds",
  duplicateVerdict([], SERIAL, null).action === "proceed");

check("a missing list proceeds rather than throwing",
  duplicateVerdict(undefined, SERIAL, null).action === "proceed");

// The row minted for the TLS token before first contact.
check("a row that never registered proceeds",
  duplicateVerdict([neverUp], SERIAL, null).action === "proceed");

check("a live device stops",
  duplicateVerdict([live, other], SERIAL, null).action === "stop");

check("a stopped verdict names the device, for the message and the buttons",
  duplicateVerdict([live], SERIAL, null).device.device_id === SERIAL);

check("the operator's choice lets the same device through",
  duplicateVerdict([live], SERIAL, SERIAL).action === "keep");

// The flag lives for the wizard session, so it outlives the device it was set
// for. Waving a DIFFERENT device through on it would delete-and-reprovision
// the one the operator never looked at.
check("a choice made for another device does not wave this one through",
  duplicateVerdict([live], SERIAL, other.device_id).action === "stop");

check("an empty choice is not a choice",
  duplicateVerdict([live], SERIAL, "").action === "stop");

// device_id has carried a prefix on some fleets; the registry matches by
// containment and this must agree with it.
const prefixed = { device_id: `em-${SERIAL}`, firmware_ver: "v2.16.0" };
check("a device_id containing the serial still matches",
  duplicateVerdict([prefixed], SERIAL, null).action === "stop");
check("and keeping it compares against the full device_id",
  duplicateVerdict([prefixed], SERIAL, `em-${SERIAL}`).action === "keep");

check("a null entry in the list does not throw",
  duplicateVerdict([null, live], SERIAL, null).action === "stop");

process.exit(fails ? 1 : 0);
