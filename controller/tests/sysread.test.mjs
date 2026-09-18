// Tests for _sysreadScript() in dashboard.jsx — the shell the connect step
// runs to read the FireOS build off /system while the device sits in TWRP.
//
//     node controller/tests/sysread.test.mjs
//
// Source extraction rather than import, for the reason boot_target.test.mjs
// gives: the dashboard compiles to a single classic script with no module
// boundary, so the alternative is a second copy that drifts.
//
// This is here because both faults it covers are SILENT on hardware. The
// wizard warns that the FireOS build is unknown and carries on, so a device
// whose identity can never be read looks exactly like a device with an
// unusual build — which is how #517 survived: globbing only the long by-name
// path meant NO amonet v2 device could be identified, and it read as a quirk
// of one unit for weeks.
//
// The script is checked rather than executed. Running it needs a device in
// recovery; what can be verified off-target is that it looks in both places a
// system partition is named and at both paths build.prop lives at.

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

const { _sysreadScript } = await import(
  "data:text/javascript;base64," + Buffer.from(
    liftFunction("_sysreadScript") + "\nexport { _sysreadScript };"
  ).toString("base64"));

const script = _sysreadScript();

let failures = 0;
function check(what, ok) {
  if (ok) { console.log(`  ok   ${what}`); return; }
  console.log(`  FAIL ${what}`);
  failures++;
}

console.log("_sysreadScript():");

// Fault 1. amonet v2's TWRP has only /dev/block/by-name; v1 has both. Looking
// in the long path alone left the node empty, the script took its early exit
// before echoing the sentinel, and readFireosBuild returned null for every v2
// device. Same assumption #513 fixed in classifyBootTarget's probe — one wrong
// belief at two call sites, which is why this asserts on the OTHER one too.
check("searches the short by-name directory (amonet v2 has only this one)",
      script.includes("/dev/block/by-name"));
check("searches the long by-name directory (amonet v1 and Android)",
      script.includes("/dev/block/platform/*/by-name"));
check("searches both in one loop, so neither layout is a special case",
      /for d in \/dev\/block\/platform\/\*\/by-name \/dev\/block\/by-name/
        .test(script));

// The slot matters: reading system_a on a device booted from b reports the
// wrong userspace and looks entirely healthy.
check("prefers the booted slot over a hardcoded system_a",
      script.includes('ro.boot.slot_suffix') && script.includes('"system$SLOT"'));

// Fault 2. FireOS 6 is system-as-root: the tree is a /system DIRECTORY inside
// the partition, so the file is one level down from wherever the partition is
// mounted. FireOS 5 keeps it at the partition root. Both have to work.
check("reads the nested FireOS 6 path", script.includes('$M/system/build.prop'));
check("still reads the flat FireOS 5 path", script.includes('B="$M/build.prop"'));
check("prefers nested only when present, rather than assuming either",
      script.includes('[ -f "$M/system/build.prop" ] && B="$M/system/build.prop"'));
check("greps the resolved path, not a literal", /"\$B"/.test(script));
check("reports which path it used, so a transcript says which layout was seen",
      script.includes('echo "PROP=$B"') && script.includes('echo "MNT=$M"'));

// Fault 3. TWRP 3.7 (amonet v2) makes /system a SYMLINK to
// /system_root/system, which does not exist until something is mounted, so
// mounting on /system failed with ENOENT and the read came back empty on every
// v2 device — measured on the spare 2026-09-17. Mount on a private directory
// instead, and never on /system.
check("never mounts on /system", !/mount[^|;]*\s\/system(\s|"|;|$)/.test(script));
check("mounts read-only on a private directory",
      script.includes('mount -o ro "$S" "$M"') && script.includes("M=/tmp/em_sysread"));
check("reads an existing mount in place instead of mounting twice",
      script.includes('mount | sed -n "s|^$S on'));
check("unmounts only what it mounted", script.includes('[ -n "$OWN" ] && { umount "$M"'));
check("a failed mount is reported, not silent", script.includes('|| echo "MOUNTFAIL"'));

// The sentinel is how readFireosBuild tells "ran and found nothing" from
// "never ran". Without it a shell that died early parses as a clean read.
check("ends with the sentinel readFireosBuild checks for",
      script.trim().endsWith("echo _SYSREAD_OK"));

if (failures) {
  console.log(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
