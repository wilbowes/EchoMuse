// Tests for classifyBootTarget() in dashboard.jsx — the check that decides
// where the provisioning wizard is allowed to write a kernel.
//
//     node controller/tests/boot_target.test.mjs
//
// Source extraction rather than import, for the reason wifi_scan.test.mjs
// gives: the dashboard compiles to a single classic script with no module
// boundary, so the alternative is a second copy that drifts.
//
// What is under test is the only partition write the wizard performs. The
// layers below FireOS on this device (preloader, LK, and amonet's unlock
// payload) are ones EchoMuse does not write. A kernel written over the payload
// costs the unlock and means running amonet again, which is not a state
// anyone can be talked out of over an issue thread.
//
// Both fixtures below are real output, read off hardware on 2026-08-08. They
// are here because the first version of this guard was written against the
// Android map, was therefore inverted for the environment the wizard actually
// runs in, and passed anyway on the accident that p10 answers to two names
// and the last one written happened to be the safe one.

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

const { classifyBootTarget } = await import(
  "data:text/javascript;base64," + Buffer.from(
    liftFunction("classifyBootTarget") + "\nexport { classifyBootTarget };"
  ).toString("base64"));
const { patchBootCmdline } = await import(
  "data:text/javascript;base64," + Buffer.from(
    liftFunction("patchBootCmdline") + "\nexport { patchBootCmdline };"
  ).toString("base64"));

// TWRP's map. The bare names are remapped onto the KERNEL partitions and the
// unlock payload is exposed explicitly as *_amonet. Both by-name directories
// exist and resolve identically, hence the duplicate lines.
const TWRP = [
  ["boot_a", "p10"], ["boot_a_amonet", "p17"], ["boot_a_x", "p10"],
  ["boot_b", "p11"], ["boot_b_amonet", "p18"], ["boot_b_x", "p11"],
].flatMap(([n, p]) => [
  `NAME ${n} /dev/block/mmcblk0${p}`,
  `NAME ${n} /dev/block/mmcblk0${p}`,
]).join("\n");

// Android's map, for contrast: no _amonet entries at all, and the bare names
// point at the payload. The wizard never runs here, but a rule that cannot
// tell the two apart is a rule that got lucky.
const ANDROID = [
  ["boot_a", "p17"], ["boot_a_x", "p10"],
  ["boot_b", "p18"], ["boot_b_x", "p11"],
].map(([n, p]) => `NAME ${n} /dev/block/mmcblk0${p}`).join("\n");

const probe = (target, names = TWRP, isBlock = true) =>
  `TARGET=${target}\nISBLK=${isBlock ? "yes" : "no"}\n${names}`;

let failures = 0;
function check(name, cond, detail) {
  if (cond) return;
  failures++;
  console.error(`FAIL: ${name}${detail ? `\n      ${detail}` : ""}`);
}

// The cmdline is a 512-byte NUL-terminated field at offset 64. The wizard
// needs one extra argument; everything FireOS already put there remains part
// of the image, and bytes outside the field are not part of this operation.
{
  const original = "bootopt=64S3,32N2,64N2 rootwait ro init=/init buildvariant=user";
  const image = new Uint8Array(640).fill(0xa5);
  image.fill(0, 64, 576);
  image.set(new TextEncoder().encode(original), 64);
  const before = new Uint8Array(image);
  const patched = patchBootCmdline(image);
  const end = patched.indexOf(0, 64);
  const cmdline = new TextDecoder().decode(patched.slice(64, end));

  check("the FireOS cmdline is preserved",
        cmdline === `${original} androidboot.selinux=permissive`, cmdline);
  check("patchBootCmdline does not mutate its input",
        image.every((byte, i) => byte === before[i]));
  check("the boot header before the cmdline is untouched", patched[63] === 0xa5);
  check("the boot image after the cmdline is untouched", patched[576] === 0xa5);
}

// Exact-token matching avoids treating a different value as already patched,
// while still making a repeat invocation idempotent.
{
  const original = "rootwait androidboot.selinux=permissive ro init=/init";
  const image = new Uint8Array(576);
  image.set(new TextEncoder().encode(original), 64);
  const patched = patchBootCmdline(image);
  const cmdline = new TextDecoder().decode(patched.slice(64)).replace(/\0.*$/s, "");
  check("an existing permissive argument is not duplicated", cmdline === original, cmdline);
}

// A full field must be refused. Truncation would silently remove a FireOS
// argument, which is the same invariant violation as replacing the field.
{
  const image = new Uint8Array(576);
  image.set(new TextEncoder().encode("x".repeat(490)), 64);
  let error = "";
  try { patchBootCmdline(image); } catch (e) { error = e.message; }
  check("a cmdline that cannot fit is refused", /too long/i.test(error), error);
}

// The normal case, and the one measured in TWRP: other-boot resolves to p10,
// which answers to both boot_a and boot_a_x.
{
  const r = classifyBootTarget(probe("/dev/block/mmcblk0p10"));
  check("TWRP p10 is accepted", r.ok === true, JSON.stringify(r));
  check("TWRP p10 reports both its aliases",
        r.names.join(",") === "boot_a,boot_a_x", JSON.stringify(r.names));
  check("TWRP p10 is not warned about", !r.warn, JSON.stringify(r));
}

// The B slot's kernel, same shape.
{
  const r = classifyBootTarget(probe("/dev/block/mmcblk0p11"));
  check("TWRP p11 is accepted", r.ok === true, JSON.stringify(r));
  check("TWRP p11 reports both its aliases",
        r.names.join(",") === "boot_b,boot_b_x", JSON.stringify(r.names));
}

// The case the guard exists for. Under TWRP the payload is named explicitly.
for (const [part, dev] of [["boot_a_amonet", "/dev/block/mmcblk0p17"],
                           ["boot_b_amonet", "/dev/block/mmcblk0p18"]]) {
  const r = classifyBootTarget(probe(dev));
  check(`TWRP ${part} is refused`, r.ok === false, JSON.stringify(r));
  check(`${part} refusal names the partition`, r.reason.includes(part), r.reason);
  check(`${part} refusal mentions the unlock`, /unlock/i.test(r.reason), r.reason);
  check(`${part} refusal says nothing was written`,
        /nothing has been written/.test(r.reason), r.reason);
}

// Android's map. The bare name is the payload and there is no _x alias on it,
// so it must be refused despite being called boot_a.
for (const [part, dev] of [["boot_a", "/dev/block/mmcblk0p17"],
                           ["boot_b", "/dev/block/mmcblk0p18"]]) {
  const r = classifyBootTarget(probe(dev, ANDROID));
  check(`Android ${part} is refused`, r.ok === false, JSON.stringify(r));
  check(`Android ${part} refusal explains the layout`,
        /_x/.test(r.reason), r.reason);
}

// Android's kernel partitions are still fine under Android's map.
{
  const r = classifyBootTarget(probe("/dev/block/mmcblk0p10", ANDROID));
  check("Android p10 is accepted", r.ok === true, JSON.stringify(r));
  check("Android p10 is named boot_a_x",
        r.names.join(",") === "boot_a_x", JSON.stringify(r.names));
}

// No symlink at all. dd would create an ordinary file in TWRP's tmpfs and
// nothing would reach flash, so this is refused rather than warned about.
{
  const r = classifyBootTarget(probe("", TWRP, false));
  check("an unresolved target is refused", r.ok === false, JSON.stringify(r));
}

// Resolves to a name, but not to a block device.
{
  const r = classifyBootTarget(probe("/dev/block/other-boot", TWRP, false));
  check("a non-block target is refused", r.ok === false, JSON.stringify(r));
  check("non-block refusal says so", /not a block device/.test(r.reason), r.reason);
}

// by-name unreadable, or laid out differently. Not evidence of danger, so it
// continues with a warning — the same reading the OTA free-space check gives
// an unreadable df. Refusing here would block a device whose TWRP differs.
{
  const r = classifyBootTarget(probe("/dev/block/mmcblk0p10", ""));
  check("an unidentifiable target continues", r.ok === true, JSON.stringify(r));
  check("an unidentifiable target warns", r.warn === true, JSON.stringify(r));
}

// A real block device that is simply not in the boot set.
{
  const r = classifyBootTarget(probe("/dev/block/mmcblk0p23"));
  check("an unrelated block device continues with a warning",
        r.ok === true && r.warn === true, JSON.stringify(r));
}

// The verdict must not depend on which alias the probe happened to list last.
// This is the accident the first version of the guard survived on.
{
  const reversed = TWRP.split("\n").reverse().join("\n");
  const a = classifyBootTarget(probe("/dev/block/mmcblk0p10", reversed));
  const b = classifyBootTarget(probe("/dev/block/mmcblk0p17", reversed));
  check("alias order does not change the kernel verdict", a.ok === true, JSON.stringify(a));
  check("alias order does not change the payload verdict", b.ok === false, JSON.stringify(b));
}

// The verdict is only worth as much as the node it is spent on. runPatchBoot
// used to classify /dev/block/other-boot and then read, write and read back
// through that same symlink rather than through the resolved target it had
// just been given a verdict about — so the safety check and the write asked
// two different questions moments apart, the flash log claimed boot.target
// whichever answer it got, and the read-back would confirm a wrong-but-stable
// resolution by reading exactly what it had written.
//
// Comments are stripped first. A guard that greps for the thing it forbids
// otherwise finds the comment explaining why it is forbidden and passes — the
// mistake this repo has now made three times.
{
  const body = liftFunction("runPatchBoot")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^[ \t]*\/\/.*$/gm, "");
  check("runPatchBoot resolves the boot partition exactly once",
        (body.match(/other-boot/g) || []).length === 1,
        "other-boot should appear only in the probe that resolves it, "
        + "never in a dd that follows the classifyBootTarget verdict");
  for (const [what, re] of [
    ["reads", /dd if=\$\{boot\.target\} of=\/tmp\/work\/boot\.img/],
    ["writes", /dd if=\/tmp\/work\/new-boot\.img of=\$\{boot\.target\}/],
    ["reads back", /dd if=\$\{boot\.target\} bs=1 skip=64/],
  ]) {
    check(`runPatchBoot ${what} the classified target`, re.test(body), body.slice(0, 0));
  }
}

if (failures) {
  console.error(`\n${failures} check(s) failed.`);
  process.exit(1);
}
console.log("boot_target: all checks passed.");
