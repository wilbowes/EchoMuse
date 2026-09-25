// Tests for the emOS flow's donor gate in dashboard.jsx — _emosSystemFiles,
// _donorProbeScript, parseDonorProbe, _kernelArchOf and donorVerdict: whether
// an Echo is in a state an emOS image can be built from (#619).
//
//     node controller/tests/donor_gate.test.mjs
//
// Source extraction rather than import, for the reason boot_target.test.mjs
// gives: the dashboard compiles to one classic script with no module boundary.
//
// #619 is the case under test: amonet 2 with a FireOS 6 flash that never
// finished, so boot_b and system_b still held FireOS 5. Every existing check
// passed because each looked at one thing, the wizard built a consistent
// FireOS 5 image, and amonet 2's bootloader boot-looped on it. The rule is that
// the unlock, the recovery, both system partitions and every stock kernel must
// be READ and must agree, and that anything unreadable refuses.
//
// Kernel headers are synthesised: a real one would put Amazon's code in this
// repository.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import { gzipSync } from "zlib";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");
const initC = readFileSync(join(HERE, "..", "..", "emos", "init", "init.c"), "utf8");

function liftFunction(name) {
  const re = new RegExp(`(async )?function ${name}\\(`);
  const m = src.match(re);
  if (!m) {
    throw new Error(`dashboard.jsx no longer defines ${name}() — if it was `
                  + `renamed or moved, update this test to match`);
  }
  const start = m.index;
  let depth = 0, i = src.indexOf("{", src.indexOf(")", start));
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) break; }
  }
  return src.slice(start, i + 1);
}

const names = ["_emosSystemFiles", "_donorProbeScript", "parseDonorProbe",
               "_kernelArchOf", "donorVerdict"];
const mod = await import("data:text/javascript;base64," + Buffer.from(
  names.map(liftFunction).join("\n") + `\nexport { ${names.join(", ")} };`
).toString("base64"));
const { _emosSystemFiles, _donorProbeScript, parseDonorProbe, _kernelArchOf,
        donorVerdict } = mod;

let fails = 0;
function check(what, cond) {
  if (cond) console.log(`ok    ${what}`);
  else { console.log(`FAIL  ${what}`); fails++; }
}

const FILES = _emosSystemFiles();

// ── the required files are the ones init actually runs ──────────────────────
{
  // Every binary on either list is one init.c names, at the path the layout
  // puts it (FireOS 6 is system-as-root, so its tree is bound at /system and
  // the files are the same relative paths). Firmware and the linker are loaded
  // by the kernel and by every dynamic binary, not named by init, so they are
  // checked by directory.
  for (const gen of [5, 6]) {
    for (const f of FILES[gen]) {
      if (f === "bin/linker") continue;
      const named = f.includes("firmware/")
        ? initC.includes(`"/system/${f.slice(0, f.lastIndexOf("/") + 1)}"`)
        : initC.includes(`"/system/${f}"`);
      check(`FireOS ${gen}: ${f} is what init.c uses`, named);
    }
  }
  check("FireOS 6 needs no Amazon supplicant (emOS ships its own)",
        !FILES[6].includes("bin/wpa_supplicant"));
  check("both generations need e2fsck, which checks /data every boot",
        FILES[5].includes("bin/e2fsck") && FILES[6].includes("bin/e2fsck"));
}

// ── the probe script ────────────────────────────────────────────────────────
{
  const s = _donorProbeScript(FILES);
  check("finds partitions by the kernel's GPT name first",
        s.includes("/sys/block/mmcblk0/mmcblk0p*/uevent") && s.includes('PARTNAME=$1')
        && s.indexOf("PARTNAME=") < s.indexOf("/dev/block/by-name"));
  check("falls back to both by-name directories",
        s.includes("/dev/block/platform/*/by-name") && s.includes("/dev/block/by-name"));
  check("never needs od on the device (TWRP 3.2.3 read expdb as unreadable through it)",
        !s.includes(" od "));
  check("resolves expdb and both system partitions through the same lookup",
        s.includes("$(part expdb)") && s.includes("$(part system_$x)"));
  check("reads BOTH system partitions", s.includes("for x in a b"));
  check("mounts read-only", s.includes("mount -o ro"));
  check("tests files are non-empty, not merely present", s.includes('[ -s "$R/$f" ]'));
  check("checks the nested (FireOS 6) layout before the root one",
        s.indexOf("$M/system/build.prop") < s.indexOf('"$M/build.prop"'));
  for (const f of new Set([...FILES[5], ...FILES[6]])) {
    check(`asks about ${f}`, s.includes(f));
  }
  check("ends with a sentinel, so a cut-off probe is not a clean one",
        s.trim().endsWith("echo _DONORPROBE_OK"));
}

// ── probe output ────────────────────────────────────────────────────────────
function sysLines(x, { node = `/dev/block/mmcblk0p1${x === "a" ? 3 : 4}`, mount = "ok",
                        layout, release, name, build = "123", files, missing = [] }) {
  const out = [`SYS_${x}_node=${node}`];
  if (!node) return out;
  out.push(`SYS_${x}_mount=${mount}`);
  if (mount !== "ok") return out;
  out.push(`SYS_${x}_layout=${layout}`, `SYS_${x}_release=${release}`,
           `SYS_${x}_name=${name}`, `SYS_${x}_incremental=${build}`);
  for (const f of new Set([...FILES[5], ...FILES[6]])) {
    out.push(`FILE_${x} ${f} ${files.includes(f) && !missing.includes(f) ? "yes" : "no"}`);
  }
  return out;
}
const FOS6 = { layout: "nested", release: "7.1.2", name: "Fire OS 6.5.7.4 (NS6574/7623)",
               files: FILES[6] };
const FOS5 = { layout: "root", release: "5.1.1", name: "Fire OS 5.5.5.4 (680767620)",
               files: FILES[5] };
function probe({ expdb = "88168858", twrp = "3.7.0_9-0", a = FOS6, b = FOS6, done = true }) {
  const lines = [`EXPDB=${expdb}`, `TWRP=${twrp}`, ...sysLines("a", a), ...sysLines("b", b)];
  if (done) lines.push("_DONORPROBE_OK");
  return parseDonorProbe(lines.join("\n"));
}

{
  const p = probe({});
  check("parses the unlock evidence", p.expdb === "88168858" && p.twrp === "3.7.0_9-0");
  check("parses where expdb is", parseDonorProbe("EXPDBDEV=/dev/block/mmcblk0p7\n").expdbDev
        === "/dev/block/mmcblk0p7");
  check("parses a system slot", p.sys.a.layout === "nested" && p.sys.a.release === "7.1.2"
        && p.sys.a.files["vendor/bin/wmt_loader"] === "yes");
  check("a probe without its sentinel is incomplete", !probe({ done: false }).complete);
  check("empty output is incomplete, never usable", !parseDonorProbe("").complete);
}

// ── kernel architecture, by the builder's own rule ──────────────────────────
function bootImage(kernel) {
  const img = new Uint8Array(2048 + 0x200 + kernel.length);
  img.set(new TextEncoder().encode("ANDROID!"), 0);
  const dv = new DataView(img.buffer);
  dv.setUint32(8, 0x200 + kernel.length, true);
  dv.setUint32(36, 2048, true);                    // page size
  dv.setUint32(2048, 0x58881688, true);            // MTK header magic
  img.set(new TextEncoder().encode("KERNEL"), 2048 + 8);
  img.set(kernel, 2048 + 0x200);
  return img;
}
{
  const z = new Uint8Array(0x100);
  new DataView(z.buffer).setUint32(0x24, 0x016f2818, true);
  check("a zImage is 32-bit", await _kernelArchOf(bootImage(z)) === "arm");

  // A real kernel is megabytes; the gate reads the first 64KB, so the gzip
  // stream it sees is cut off. Build one large enough to span several deflate
  // blocks and truncate it the same way.
  const image = new Uint8Array(1 << 20);
  for (let i = 0; i < image.length; i++) image[i] = (i * 2654435761) >>> 24;
  image.set([0x41, 0x52, 0x4d, 0x64], 0x38);       // "ARM\x64"
  const cut = bootImage(gzipSync(image)).subarray(0, 65536);
  check("a truncated gzip AArch64 Image is 64-bit", await _kernelArchOf(cut) === "arm64");

  const notArm = new Uint8Array(1 << 16);
  const cut2 = bootImage(gzipSync(notArm)).subarray(0, 65536);
  check("gzip that is not an AArch64 Image is unidentified", await _kernelArchOf(cut2) === "");
  check("no MTK header is unidentified", await _kernelArchOf((() => {
    const b = bootImage(z); new DataView(b.buffer).setUint32(2048, 0, true); return b; })()) === "");
  check("not a boot image is unidentified", await _kernelArchOf(new Uint8Array(4096)) === "");
  check("nothing read is unidentified", await _kernelArchOf(null) === "");
}

// ── the verdict ─────────────────────────────────────────────────────────────
const v = (p, layout, heads) => donorVerdict({ probe: p, layout, heads, files: FILES });
const armB = [{ name: "boot_b", arch: "arm" }];

{
  const r = v(probe({}), "v2", armB);
  check("amonet 2, FireOS 6 in both slots, 32-bit kernel: accepted", r.ok && r.gen === 6);
  check("says what it confirmed", r.confirmed.some(l => l.includes("system_b"))
        && r.confirmed.some(l => l.includes("boot_b")));
}
{
  const r = v(probe({ expdb: "00000000", twrp: "3.2.3-0", a: FOS5, b: FOS5 }), "v1",
              [{ name: "the boot image", arch: "arm64" }]);
  check("amonet 1, FireOS 5 in both slots, 64-bit kernel: accepted", r.ok && r.gen === 5);
}
{
  // C95, 2026-09-25: amonet 1, FireOS 5 in system_a, FireOS 6 in system_b. The
  // image mounts system_a only, so system_b is reported and cannot block.
  const v1 = (a, b) => v(probe({ expdb: "00000000", twrp: "3.2.3-0", a, b }), "v1",
                         [{ name: "the boot image", arch: "arm64" }]);
  const r = v1(FOS5, FOS6);
  check("C95: amonet 1 with FireOS 6 in the unused system_b is accepted", r.ok && r.gen === 5);
  check("C95: system_b is reported as a warning", r.notes.some(n =>
        n.includes("system_b holds Fire OS 6.5.7.4") && n.includes("not a reason to stop")));
  check("C95: system_a is what it confirmed", r.confirmed.some(l => l.startsWith("system_a:")));
  check("amonet 1 with FireOS 6 in system_a (what it mounts): refused", !v1(FOS6, FOS5).ok);
  const moved = v1({ ...FOS5, node: "/dev/block/mmcblk0p14" }, FOS5);
  check("amonet 1 whose system_a is not mmcblk0p13: refused",
        !moved.ok && moved.reason.includes("mounts /dev/block/mmcblk0p13"));
  check("amonet 1, system_b missing entirely: accepted, noted",
        v1(FOS5, { ...FOS5, node: "" }).ok);
}
{
  // #619 as reported: boot_a had already been written by an earlier attempt,
  // so boot_b was the only stock image, and it and system_b were FireOS 5.
  const r = v(probe({ b: FOS5 }), "v2", [{ name: "boot_b", arch: "arm64" }]);
  check("#619: refused", !r.ok);
  check("#619: names system_b", /system_b holds Fire OS 5\.5\.5\.4.*FireOS 5, not FireOS 6/.test(r.reason));
  check("#619: names the kernel", /boot_b holds a 64-bit \(FireOS 5\) kernel/.test(r.reason));
  check("#619: says nothing was written", r.reason.includes("Nothing has been written"));
  check("#619: says to finish FireOS 6 in both slots", r.reason.includes("BOTH slots"));
}
{
  const r = v(probe({}), "v2", [{ name: "boot_a", arch: "arm" }, { name: "boot_b", arch: "arm64" }]);
  check("a FireOS 5 kernel in the slot KEPT for recovery refuses too", !r.ok
        && r.reason.includes("boot_b holds a 64-bit"));
}
{
  const r = v(probe({ twrp: "3.2.3-0" }), "v2", armB);
  check("expdb says amonet 2 but TWRP says amonet 1: refused", !r.ok
        && r.reason.includes("does not add up"));
}
{
  const r = v(probe({ expdb: "00000000" }), "v2", armB);
  check("TWRP 3.7 but no amonet 2 bootloader in expdb: refused", !r.ok);
}
{
  const r = v(probe({}), "v1", armB);
  check("amonet 2 evidence on a v1 partition layout: refused", !r.ok);
}
{
  check("TWRP 3.7.0_9-0 is matched despite the underscore",
        v(probe({ twrp: "3.7.0_9-0" }), "v2", armB).ok);
  check("TWRP 3.7.10 is not 3.7.0", !v(probe({ twrp: "3.7.10" }), "v2", armB).ok);
}
{
  const ru = v(probe({ expdb: "" }), "v2", armB);
  check("expdb unreadable: refused", !ru.ok);
  check("an unreadable fact is reported as the wizard's failure, not the unlock's",
        ru.reason.includes("could not read the expdb") && ru.reason.includes("Try this step again")
        && !/unlock completed/.test(ru.reason));
  const rd = v(probe({ twrp: "3.2.3-0" }), "v2", armB);
  check("a disagreement names the combinations that are built for",
        rd.reason.includes("amonet 1 with TWRP 3.2.3") && rd.reason.includes("amonet 2 with TWRP 3.7.0"));
  check("TWRP unreadable: refused", !v(probe({ twrp: "" }), "v2", armB).ok);
  check("kernel unidentified: refused",
        !v(probe({}), "v2", [{ name: "boot_b", arch: "" }]).ok);
  check("no stock image at all: refused", !v(probe({}), "v2", []).ok);
  check("a cut-off probe: refused", !v(probe({ done: false }), "v2", armB).ok);
}
{
  const r = v(probe({ b: { ...FOS6, node: "" } }), "v2", armB);
  check("system_b missing: refused", !r.ok && r.reason.includes("system_b does not exist"));
  const r2 = v(probe({ a: { ...FOS6, mount: "fail" } }), "v2", armB);
  check("system_a will not mount: refused", !r2.ok && r2.reason.includes("would not mount"));
  const r3 = v(probe({ a: { ...FOS6, layout: "none", release: "" } }), "v2", armB);
  check("no build.prop: refused", !r3.ok && r3.reason.includes("no readable build.prop"));
}
{
  const r = v(probe({ a: { ...FOS6, missing: ["vendor/firmware/WMT_SOC.cfg"] } }), "v2", armB);
  check("a missing file emOS needs: refused, naming it", !r.ok
        && r.reason.includes("vendor/firmware/WMT_SOC.cfg"));
}
{
  // A FireOS 6 release number with a FireOS 5 layout is not FireOS 6.
  const r = v(probe({ a: { ...FOS6, layout: "root" } }), "v2", armB);
  check("release and layout must BOTH say FireOS 6", !r.ok);
}

if (fails) { console.log(`\n${fails} failure(s)`); process.exit(1); }
console.log("\nall passed");
