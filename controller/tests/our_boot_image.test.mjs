// Tests for isOurBootImage() in dashboard.jsx — whether a boot slot holds an
// emOS image, read from its cmdline.
//
//     node controller/tests/our_boot_image.test.mjs
//
// Source extraction rather than import, for the reason the other dashboard
// tests give: the dashboard compiles to a single classic script with no module
// boundary, so the alternative is a second copy that drifts.
//
// The FireOS flow patches whatever is in the slot with Magisk. On an emOS
// image that bootloops the device — reported and reproduced 2026-09-20 — and
// step 1's FireOS 5 check cannot catch it, because emOS mounts FireOS's
// /system and build.prop reports 5.1.1.
//
// The markers must stay in step with the emOS flow's slot probe in
// runEscrowBoot, which is why the last test reads them back out of the source.

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

const isOurBootImage = eval(`(${liftFunction("isOurBootImage")})`);

let fails = 0;
function check(what, cond) {
  if (!cond) { console.error(`FAIL ${what}`); fails++; }
  else console.log(`ok   ${what}`);
}

// A real stock FireOS 5 cmdline, as read off the device.
const STOCK = "console=tty0 console=ttyMT0,921600n1 androidboot.hardware=mt8163 "
            + "bootopt=64S3,32N2,64N2 androidboot.selinux=enforce "
            + "androidboot.slot_suffix=_a";

check("a stock cmdline is not ours", isOurBootImage(STOCK) === false);

check("an empty cmdline is not ours", isOurBootImage("") === false);
check("a missing cmdline is not ours", isOurBootImage(undefined) === false);
check("a null cmdline is not ours", isOurBootImage(null) === false);

// Current packer output: stamped.
check("a stamped image is ours",
  isOurBootImage(STOCK + " emos.system=/dev/block/mmcblk0p13") === true);

// The case the second marker exists for: emOS images built before the stamp.
// EFF ran one of these — 0.3, no stamp — and reading it as stock is what would
// have patched it.
check("an unstamped emOS image is still ours",
  isOurBootImage(STOCK + " ramoops.mem_address=0x44400000 "
               + "ramoops.mem_size=0x200000") === true);

// The full address, not the bare key: a vendor image reserving a DIFFERENT
// ramoops region must not read as ours, or the wizard refuses a stock device.
check("another ramoops region is not ours",
  isOurBootImage(STOCK + " ramoops.mem_address=0x50000000") === false);

check("the bare key alone is not ours",
  isOurBootImage(STOCK + " ramoops.mem_size=0x200000") === false);

// The emOS flow's slot probe classifies with the same two markers. If one side
// gains a marker the other must too, or the two flows disagree about the same
// partition.
const probe = src.slice(src.indexOf("'for x in a b; do '"));
check("the escrow probe still matches on emos.system=",
  probe.includes("*emos.system=*"));
check("the escrow probe still matches on the full ramoops address",
  probe.includes("*ramoops.mem_address=0x44400000*"));

process.exit(fails ? 1 : 0);
