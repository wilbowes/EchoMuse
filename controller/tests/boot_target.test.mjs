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

// First occurrence wins, so replace each enforce token where it appears.
// Keep whitespace, unrelated arguments and bytes outside the field intact.
for (const original of [
  "rootwait androidboot.selinux=enforce ro init=/init",
  "androidboot.selinux=enforce androidboot.selinux=permissive",
  "androidboot.selinux=permissive androidboot.selinux=enforce",
  "androidboot.selinux=enforce androidboot.selinux=enforce",
  "  rootwait\tandroidboot.selinux=enforce\t ro  ",
]) {
  const image = new Uint8Array(640).fill(0xa5);
  image.fill(0, 64, 576);
  image.set(new TextEncoder().encode(original), 64);
  const before = new Uint8Array(image);
  let patched;
  try { patched = patchBootCmdline(image); } catch (e) {
    check("an enforce token is replaced instead of refused", false, e.message);
    continue;
  }
  const end = patched.indexOf(0, 64);
  const expected = original.replaceAll("androidboot.selinux=enforce", "androidboot.selinux=permissive");
  check("enforce tokens are replaced in place",
        new TextDecoder().decode(patched.slice(64, end)) === expected, original);
  check("replacement does not mutate the input", image.every((byte, i) => byte === before[i]));
  check("replacement leaves all bytes outside the field untouched",
        patched.every((byte, i) => (i >= 64 && i < 576) || byte === before[i]));
  check("replacement is idempotent",
        patchBootCmdline(patched).every((byte, i) => byte === patched[i]));
}

// Replacement may grow the field, but must still leave a NUL terminator.
for (const outputLength of [511, 512]) {
  const original = "x".repeat(outputLength - " androidboot.selinux=permissive".length)
    + " androidboot.selinux=enforce";
  const image = new Uint8Array(640).fill(0xa5);
  image.fill(0, 64, 576);
  image.set(new TextEncoder().encode(original), 64);
  const before = new Uint8Array(image);
  let patched, error = "";
  try { patched = patchBootCmdline(image); } catch (e) { error = e.message; }
  if (outputLength === 511) {
    check("replacement fits exactly with its terminator", !!patched && patched[575] === 0, error);
    check("exact-fit replacement preserves the following byte", !!patched && patched[576] === 0xa5);
  } else {
    check("replacement without space for a terminator is refused", /too long/i.test(error), error);
  }
  check("boundary handling does not mutate the input", image.every((byte, i) => byte === before[i]));
}

// A token-like substring in an unrelated argument is not rewritten. Even
// non-UTF-8 bytes in those arguments must survive replacement byte-for-byte.
{
  const original = new TextEncoder().encode("x=androidboot.selinux=enforce rootwait androidboot.selinux=enforce");
  original[0] = 0xff;
  const image = new Uint8Array(576);
  image.set(original, 64);
  let patched;
  try { patched = patchBootCmdline(image); } catch (e) {
    check("replacement accepts unrelated opaque bytes", false, e.message);
  }
  const prefixLength = "x=androidboot.selinux=enforce rootwait ".length;
  check("unrelated bytes survive replacement exactly", !!patched &&
        original.slice(0, prefixLength).every((byte, i) => patched[64 + i] === byte));
}

// Unsupported values still require an explicit decision instead of guessing.
{
  const image = new Uint8Array(576);
  image.set(new TextEncoder().encode("androidboot.selinux=unknown"), 64);
  let error = "";
  try { patchBootCmdline(image); } catch (e) { error = e.message; }
  check("an unknown SELinux value is refused", /conflicting/.test(error), error);
}

// A full field without a terminator cannot be returned as an already-patched image.
{
  const image = new Uint8Array(576).fill(0x78);
  image.set(new TextEncoder().encode("androidboot.selinux=permissive "), 64);
  let error = "";
  try { patchBootCmdline(image); } catch (e) { error = e.message; }
  check("a missing terminator is refused", /terminator/i.test(error), error);
}

// Both append and replacement obey the same 511-byte payload limit.
for (const outputLength of [511, 512]) {
  const original = "x".repeat(outputLength - " androidboot.selinux=permissive".length);
  const image = new Uint8Array(576);
  image.set(new TextEncoder().encode(original), 64);
  let patched, error = "";
  try { patched = patchBootCmdline(image); } catch (e) { error = e.message; }
  check(`append boundary at ${outputLength} bytes`, outputLength === 511
        ? !!patched && patched[575] === 0 : /too long/i.test(error), error);
}

for (const size of [0, 63, 64, 575]) {
  let error = "";
  try { patchBootCmdline(new Uint8Array(size)); } catch (e) { error = e.message; }
  check(`an incomplete field of ${size} bytes is refused`, /too short/i.test(error), error);
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

// Resolves, but not to a block device. The path has to be something OTHER
// than other-boot itself: `readlink -f` echoes its argument back when the
// path does not exist, so other-boot resolving to other-boot means the alias
// is ABSENT, which is amonet v2 and is handled further down. amonet v2 points
// lk/preloader/tee at /tmp/ota-decoy, so a tmpfs target is a real hazard
// rather than a hypothetical one.
{
  const r = classifyBootTarget(probe("/tmp/ota-decoy/boot", TWRP, false));
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

// Drive the real caller with an in-memory device transport. A substring in
// magiskboot's log must not bypass token replacement or field validation.
for (const [original, outcome] of [
  ["androidboot.selinux=enforce androidboot.selinux=permissive", "patch"],
  ["x=androidboot.selinux=permissive androidboot.selinux=enforce", "patch"],
  ["androidboot.selinux=permissive_suffix", "reject"],
  ["androidboot.selinux=unknown androidboot.selinux=permissive", "reject"],
  ["androidboot.selinux=permissive " + "x".repeat(483), "reject"],
  ["rootwait androidboot.selinux=permissive ro", "skip"],
]) {
  const image = new Uint8Array(640);
  image.set(new TextEncoder().encode("ANDROID!"));
  image.set(new TextEncoder().encode(original), 64);
  const before = new Uint8Array(image);
  const pushes = [], commands = [], logs = [];
  const stopAfterPush = new Error("test stops before flashing");
  const c = {
    async shell(command) {
      commands.push(command);
      if (command.startsWith("d=")) return probe("/dev/block/mmcblk0p10");
      if (command.includes("magiskboot unpack")) return `CMDLINE [${original}]`;
      return "";
    },
    async pull(path) {
      if (path === "/tmp/work/boot.img") return image;
      if (path === "/tmp/ramdisk/init.csm.project.rc") {
        return new TextEncoder().encode("service echomuse");
      }
      throw new Error(`unexpected pull: ${path}`);
    },
    async push(path, bytes) {
      pushes.push({ path, bytes });
      throw stopAfterPush;
    },
  };
  // The escrow (#468) runs before anything writes; its helpers are stubbed
  // and it is recorded in the same list as pushes, so the order is checkable.
  const events = [];
  const runPatchBoot = new Function("classifyBootTarget", "patchBootCmdline", "addLog",
    "setProgress", "_INIT_RC_APPEND", "_md5Hex", "setEmosRef", "setEmosTarget",
    "_downloadBytes", `return async ${liftFunction("runPatchBoot")}`)(
      classifyBootTarget, patchBootCmdline, text => logs.push(text), () => {}, "",
      () => "0".repeat(32), () => events.push("escrow"), () => {}, () => events.push("download"));
  let error;
  try { await runPatchBoot(c); } catch (e) { error = e; }
  check("the image is escrowed and downloaded before anything is pushed",
        events[0] === "escrow" && events[1] === "download", events.join(","));
  if (outcome === "patch") {
    check("the caller pushes a corrected image", error === stopAfterPush &&
          pushes.length === 1 && pushes[0].path === "/tmp/work/boot_patched.img", original);
    if (pushes.length) {
      const expected = patchBootCmdline(image);
      check("the caller sends the bounded transformation", expected.every(
        (byte, i) => pushes[0].bytes[i] === byte));
    }
  } else if (outcome === "reject") {
    check("the caller rejects invalid cmdlines before writing", !!error &&
          /conflicting|terminator/.test(error.message) && pushes.length === 0, original);
  } else {
    check("an already patched image avoids another write", !error && pushes.length === 0 &&
          logs.includes("Boot image already fully patched — nothing to flash."), error?.message);
  }
  check("the caller leaves the pulled image unchanged", image.every((byte, i) => byte === before[i]));
  check("the test never reaches a flash command", !commands.some(command =>
        command.startsWith("dd if=/tmp/work/new-boot.img")));
}

// ── amonet v2 ────────────────────────────────────────────────────────────────
//
// Read off a v2 device in TWRP 3.7.0_9-0 on 2026-09-13. Nothing is inverted:
// there is no other-boot, no _x alias and no _amonet alias, the payload lives
// in expdb, and boot_a/boot_b point at the real kernel partitions. Only
// /dev/block/by-name exists — the platform path the v1 probe globbed does not.
//
// The active slot MOVES: installing a FireOS zip flashes the inactive slot and
// switches to it, so `ro.boot.slot_suffix` is the only thing that can answer
// which partition this device actually boots.
const V2 = [
  ["boot_a", "p10"], ["boot_b", "p11"],
].map(([n, p]) => `NAME ${n} /dev/block/mmcblk0${p}`).join("\n");

// `readlink -f` echoes its argument back when the path does not exist, which
// is what an absent other-boot looks like.
const v2probe = (suffix, names = V2, slotBlock = true, slotDev = undefined) => {
  const dev = slotDev !== undefined ? slotDev
            : (suffix === "_a" ? "/dev/block/mmcblk0p10"
             : suffix === "_b" ? "/dev/block/mmcblk0p11"
             : `/dev/block/by-name/boot${suffix}`);
  return `TARGET=/dev/block/other-boot\nISBLK=no\nSUFFIX=${suffix}\n`
       + `SLOTDEV=${dev}\nSLOTBLK=${slotBlock ? "yes" : "no"}\n${names}`;
};

// The measured device: booted slot B, so p11 and never p10.
{
  const r = classifyBootTarget(v2probe("_b"));
  check("v2 slot B is accepted", r.ok === true, JSON.stringify(r));
  check("v2 slot B targets p11", r.target === "/dev/block/mmcblk0p11", JSON.stringify(r));
  check("v2 slot B is reported as v2", r.layout === "v2", JSON.stringify(r));
  check("v2 slot B records the slot", r.slot === "_b", JSON.stringify(r));
  check("v2 slot B names the partition", r.names.join(",") === "boot_b", JSON.stringify(r.names));
  check("v2 slot B is not warned about", !r.warn, JSON.stringify(r));
}

// The other slot, since it is whichever one the last install did not write.
{
  const r = classifyBootTarget(v2probe("_a"));
  check("v2 slot A is accepted", r.ok === true, JSON.stringify(r));
  check("v2 slot A targets p10", r.target === "/dev/block/mmcblk0p10", JSON.stringify(r));
}

// No slot suffix is a REFUSAL, never a default. boot_a is the wrong answer
// half the time, and writing it leaves the device booting what it booted
// before — which reads as the flash having silently done nothing.
for (const suffix of ["", "_c", "a", "_ab"]) {
  const r = classifyBootTarget(v2probe(suffix));
  check(`v2 refuses slot suffix ${JSON.stringify(suffix)}`, r.ok === false, JSON.stringify(r));
  check(`v2 refusal for ${JSON.stringify(suffix)} says nothing was written`,
        /Nothing has been (read or )?written/.test(r.reason), r.reason);
  check(`v2 refusal for ${JSON.stringify(suffix)} does not name a target`,
        r.target !== "/dev/block/mmcblk0p10" && r.target !== "/dev/block/mmcblk0p11",
        JSON.stringify(r));
}

// A slot that resolves to something that is not a block device is the tmpfs
// case, which is never safe — amonet v2 points lk/preloader/tee at
// /tmp/ota-decoy, so a wrong name really can land there.
{
  const r = classifyBootTarget(v2probe("_b", V2, false, "/tmp/ota-decoy/boot_b"));
  check("v2 refuses a non-block slot", r.ok === false, JSON.stringify(r));
  check("v2 non-block refusal mentions tmpfs", /tmpfs/.test(r.reason), r.reason);
}

// A slot that does not resolve at all.
{
  const r = classifyBootTarget(v2probe("_b", V2, false, "/dev/block/by-name/boot_b"));
  check("v2 refuses an unresolved slot", r.ok === false, JSON.stringify(r));
  check("v2 unresolved refusal names the slot", /boot_b/.test(r.reason), r.reason);
}

// A v1-shaped map with no other-boot is a combination nothing has seen.
// Refusing costs a bug report; guessing costs the unlock.
{
  const r = classifyBootTarget(v2probe("_a", TWRP));
  check("no other-boot but v1 aliases is refused", r.ok === false, JSON.stringify(r));
  check("that refusal names the v1 aliases", /_amonet|_x/.test(r.reason), r.reason);
}

// v1 must be completely unaffected by any of the above: its probe carries no
// SUFFIX at all, and other-boot resolving to a block device still decides.
{
  const withSuffix = probe("/dev/block/mmcblk0p10") + "\nSUFFIX=\nSLOTDEV=\nSLOTBLK=no";
  const r = classifyBootTarget(withSuffix);
  check("v1 ignores an empty slot suffix", r.ok === true, JSON.stringify(r));
  check("v1 is reported as v1", r.layout === "v1", JSON.stringify(r));
  check("v1 still targets p10", r.target === "/dev/block/mmcblk0p10", JSON.stringify(r));
}

// A v1 device that DOES report a slot suffix must still go down the v1 path —
// other-boot is the discriminator, not the presence of the property.
{
  const withSuffix = probe("/dev/block/mmcblk0p11") + "\nSUFFIX=_a\nSLOTDEV=/dev/block/mmcblk0p10\nSLOTBLK=yes";
  const r = classifyBootTarget(withSuffix);
  check("v1 with a slot suffix still uses other-boot", r.ok === true, JSON.stringify(r));
  check("v1 with a slot suffix keeps other-boot's target",
        r.target === "/dev/block/mmcblk0p11", JSON.stringify(r));
}

// The probes in both flows must collect what the v2 branch reads, or it is
// dead code that refuses every v2 device. Comments stripped first, for the
// reason the runPatchBoot check below gives.
for (const fn of ["runEscrowBoot", "runPatchBoot"]) {
  // Line comments only. The block-comment pattern treats the `/*/` inside
  // /dev/block/platform/*/by-name as a comment and eats the rest of the
  // command — stripping it would fail a probe that is present and correct.
  const body = liftFunction(fn).replace(/^[ \t]*\/\/.*$/gm, "");
  for (const [what, needle] of [
    ["reads the slot suffix", "ro.boot.slot_suffix"],
    ["emits SUFFIX", "SUFFIX="],
    ["emits SLOTDEV", "SLOTDEV="],
    ["emits SLOTBLK", "SLOTBLK="],
    ["globs the short by-name path", "/dev/block/by-name/boot_*"],
  ]) {
    check(`${fn} ${what}`, body.includes(needle), `${fn} probe is missing ${needle}`);
  }
}

if (failures) {
  console.error(`\n${failures} check(s) failed.`);
  process.exit(1);
}
console.log("boot_target: all checks passed.");
