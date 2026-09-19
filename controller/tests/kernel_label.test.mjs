// Tests for _archShort() / _osLabel() in dashboard.jsx — the "emOS (64-bit)"
// label on each device.
//
//     node controller/tests/kernel_label.test.mjs
//
// The server is a 32-bit program, so on a 64-bit ARM kernel it reads "armv8l"
// from uname, never "aarch64". Reading armv8l as 32-bit labelled every FireOS 5
// emOS device wrongly and nothing failed (2026-09-19).

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

function liftFunction(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}()`);
  let depth = 0;
  for (let i = src.indexOf("{", start); i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`could not find the end of ${name}`);
}

const { _osLabel } = await import(
  "data:text/javascript;base64," + Buffer.from(
    ["_baseOsLabel", "_archShort", "_osLabel"].map(liftFunction).join("\n")
    + "\nexport { _osLabel };"
  ).toString("base64"));

let failures = 0;
function check(name, got, want) {
  if (got === want) return;
  failures++;
  console.error(`FAIL: ${name}\n      got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

const emos = arch => _osLabel({ baseOs: "emos", kernelArch: arch });
check("armv8l is a 32-bit task on a 64-bit kernel", emos("armv8l"), "emOS (64-bit)");
check("aarch64", emos("aarch64"), "emOS (64-bit)");
check("armv7l is a 32-bit kernel", emos("armv7l"), "emOS (32-bit)");
check("x86_64", emos("x86_64"), "emOS (64-bit)");
check("i686", emos("i686"), "emOS (32-bit)");
check("unknown arch passes through", emos("riscv64"), "emOS (riscv64)");
check("no kernel reported", emos(undefined), "emOS");
check("FireOS gets no arch", _osLabel({ baseOs: "fireos", kernelArch: "armv8l" }), "FireOS 5");

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("kernel_label: all checks passed");
