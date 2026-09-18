// Tests for _serverBinaryVerdict() in dashboard.jsx — whether the wizard's
// Install EchoMuse step will push a file at all.
//
//     node controller/tests/server_binary.test.mjs
//
// Source extraction rather than import, for the reason boot_target.test.mjs
// gives. The case that found it: the escrowed boot image picked as the custom
// build installed cleanly, because the step verifies the copy by size only.

import { readFileSync, existsSync } from "fs";
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

const verdict = eval(`(${liftFunction("_serverBinaryVerdict")})`);
let failed = 0;
function check(name, cond, detail) {
  if (cond) return;
  failed++;
  console.error(`FAIL: ${name}${detail ? `\n      ${detail}` : ""}`);
}

const MARK = new TextEncoder().encode("github.com/wilbowes/EchoMuse/internal/client");
function elf({ cls = 1, machine = 0x28, marker = true } = {}) {
  const b = new Uint8Array(4096);
  b.set([0x7f, 0x45, 0x4c, 0x46, cls, 1, 1]);
  b[18] = machine & 0xff; b[19] = machine >> 8;
  if (marker) b.set(MARK, 1000);
  return b;
}

check("an ARM32 EchoMuse build is accepted", verdict(elf()).ok);
const boot = new Uint8Array(4096);
boot.set(new TextEncoder().encode("ANDROID!"));
const r = verdict(boot);
check("a boot image is refused", !r.ok && /not a program/.test(r.reason), r.reason);
check("the refusal quotes what the file starts with", /ANDROID!/.test(r.reason), r.reason);
check("a 64-bit ELF is refused", !verdict(elf({ cls: 2 })).ok);
check("an x86 ELF is refused", !verdict(elf({ machine: 0x03 })).ok);
check("an ARM binary that is not ours is refused", !verdict(elf({ marker: false })).ok);
check("an empty file is refused", !verdict(new Uint8Array(0)).ok);
check("an ArrayBuffer is accepted as input", verdict(elf().buffer).ok);

// The real thing, when a build is present locally (it is not in CI).
const built = join(HERE, "..", "..", "device", "build", "server");
if (existsSync(built)) {
  check("the local server build is accepted", verdict(new Uint8Array(readFileSync(built))).ok);
}

if (failed) { console.error(`${failed} check(s) failed.`); process.exit(1); }
console.log("server_binary: all checks passed.");
