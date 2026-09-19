// Tests for _elfSymbol() and _nvramRecord() in dashboard.jsx — how the emOS
// flow rebuilds the WiFi driver's NVRAM record from the device's own
// libcustom_nvram.so (ensureWifiNvram).
//
//     node controller/tests/wifi_nvram.test.mjs
//
// Source extraction rather than import, for the reason pm_verdict.test.mjs
// gives. The library is Amazon's and is not in this repo, so the ELF here is
// synthetic; against the real FireOS 5 and 6 libraries both functions were
// checked to reproduce EFF's and VVV's WIFI file byte for byte (2026-09-19).
//
// Both faults are silent on hardware: a wrong symbol read or a wrong checksum
// writes a record the driver accepts and nobody inspects.

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

const { _elfSymbol, _nvramRecord } = await import(
  "data:text/javascript;base64," + Buffer.from(
    liftArrow("_elfSymbol") + liftArrow("_nvramRecord")
    + "\nexport { _elfSymbol, _nvramRecord };"
  ).toString("base64"));

let failures = 0;
function check(name, cond, detail) {
  if (cond) return;
  failures++;
  console.error(`FAIL: ${name}${detail ? `\n      ${detail}` : ""}`);
}
const same = (a, b) => !!a && a.length === b.length && a.every((x, i) => x === b[i]);

// A minimal 32-bit LE ELF: one PT_LOAD whose vaddr differs from its file
// offset (as in the real library, where the data segment sits 0x1000 higher),
// a .dynstr, and a .dynsym holding two symbols.
function buildElf({ loadVaddr = 0x2000, loadOff = 0x1000 } = {}) {
  const buf = new Uint8Array(0x1400);
  const dv = new DataView(buf.buffer);
  buf.set([0x7f, 0x45, 0x4c, 0x46, 1, 1, 1]);
  const PH = 0x40, SH = 0x100, STR = 0x200, SYM = 0x300;
  dv.setUint32(28, PH, true); dv.setUint32(32, SH, true);
  dv.setUint16(42, 32, true); dv.setUint16(44, 1, true);
  dv.setUint16(46, 40, true); dv.setUint16(48, 3, true);
  // PT_LOAD: type, offset, vaddr, paddr, filesz
  dv.setUint32(PH, 1, true); dv.setUint32(PH + 4, loadOff, true);
  dv.setUint32(PH + 8, loadVaddr, true); dv.setUint32(PH + 16, 0x400, true);
  const names = "\0stWifiCfgDefault\0stWifiCfgDefaultX\0";
  buf.set(new TextEncoder().encode(names), STR);
  // section 1 = .dynstr, section 2 = .dynsym (link -> 1)
  dv.setUint32(SH + 40 + 4, 3, true); dv.setUint32(SH + 40 + 16, STR, true);
  dv.setUint32(SH + 40 + 20, names.length, true);
  dv.setUint32(SH + 80 + 4, 11, true); dv.setUint32(SH + 80 + 16, SYM, true);
  dv.setUint32(SH + 80 + 20, 48, true); dv.setUint32(SH + 80 + 24, 1, true);
  // sym 1: the LONGER name first, so a prefix match would pick the wrong one
  dv.setUint32(SYM + 16, 18, true); dv.setUint32(SYM + 20, loadVaddr + 0x100, true);
  dv.setUint32(SYM + 24, 4, true);
  // sym 2: stWifiCfgDefault, 8 bytes at vaddr loadVaddr + 0x10
  dv.setUint32(SYM + 32, 1, true); dv.setUint32(SYM + 36, loadVaddr + 0x10, true);
  dv.setUint32(SYM + 40, 8, true);
  buf.set([1, 2, 3, 4, 5, 6, 7, 8], loadOff + 0x10);
  buf.set([9, 9, 9, 9], loadOff + 0x100);
  return buf;
}

// ── _elfSymbol ──
const elf = buildElf();
check("reads a symbol through the segment's vaddr-to-offset mapping",
  same(_elfSymbol(elf, "stWifiCfgDefault"), [1, 2, 3, 4, 5, 6, 7, 8]),
  String(_elfSymbol(elf, "stWifiCfgDefault")));
check("matches the whole name, not a prefix",
  same(_elfSymbol(elf, "stWifiCfgDefaultX"), [9, 9, 9, 9]));
check("an absent symbol is null", _elfSymbol(elf, "stGPSConfigDefault") === null);
check("not an ELF is null", _elfSymbol(new Uint8Array(200), "stWifiCfgDefault") === null);
const e64 = buildElf(); e64[4] = 2;
check("a 64-bit ELF is refused, not misread", _elfSymbol(e64, "stWifiCfgDefault") === null);
check("a truncated file is null, not a throw",
  _elfSymbol(elf.slice(0, 0x320), "stWifiCfgDefault") === null);
const flat = buildElf({ loadVaddr: 0x1000, loadOff: 0x1000 });
check("vaddr equal to offset also works",
  same(_elfSymbol(flat, "stWifiCfgDefault"), [1, 2, 3, 4, 5, 6, 7, 8]));

// ── _nvramRecord ──
check("an all-zero record gets aa 00 (WIFI_CUSTOM on the device)",
  same(_nvramRecord(new Uint8Array(4)), [0, 0, 0, 0, 0xaa, 0x00]));
// even index ADDS, odd index XORS: ((0+0x10)^0x01)=0x11, +0x20=0x31, ^0x03=0x32
check("even bytes add and odd bytes xor",
  same(_nvramRecord(Uint8Array.from([0x10, 0x01, 0x20, 0x03])),
       [0x10, 0x01, 0x20, 0x03, 0xaa, 0x32]));
// wraps at 8 bits: 0xff + 0xff = 0x1fe -> 0xfe
check("the checksum is 8-bit",
  _nvramRecord(Uint8Array.from([0xff, 0x00, 0xff]))[4] === 0xfe);
check("the record is the data plus two bytes", _nvramRecord(new Uint8Array(512)).length === 514);

// ── ensureWifiNvram: where it runs and what it will not do ──
const fn = src.slice(src.indexOf("async function ensureWifiNvram"),
                     src.indexOf("async function ensureWifiNvram") + 6000);
check("it checks for an existing record before anything else",
  fn.indexOf("[ -s ${REC} ]") > 0
  && fn.indexOf("[ -s ${REC} ]") < fn.indexOf("cp /tmp/em-wifi-nvram"));
check("it reads the symbol by name", fn.includes("'stWifiCfgDefault')"));
check("it reads the record back before claiming success",
  fn.indexOf("await c.pull(REC)") > 0
  && fn.indexOf("await c.pull(REC)") < fn.indexOf("written from this device"));
const emosSwitch = src.slice(src.indexOf("if (isEmos) switch (stepIdx)"),
                             src.indexOf("else switch (stepIdx)"));
const fireSwitch = src.slice(src.indexOf("else switch (stepIdx)"),
                             src.indexOf("else switch (stepIdx)") + 1500);
check("the emOS flow runs it before installing EchoMuse",
  /ensureWifiNvram\(c\);\s*\n\s*await runInstallEchoMuse/.test(emosSwitch));
check("the FireOS flow does not — nvram_daemon owns the file there",
  !fireSwitch.includes("ensureWifiNvram"));

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("wifi_nvram: all checks passed");
