// Tests for _baseOsLabel() in dashboard.jsx — the emOS / FireOS 5 slug on a
// device's tile and detail header.
//
//     node controller/tests/base_os_label.test.mjs
//
// Null must show NOTHING: old firmware cannot report its base, and a device
// that has never registered has none on record. Guessing "FireOS" there is the
// wrong answer the compatibility rules exist to prevent.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");
const start = src.indexOf("function _baseOsLabel");
if (start < 0) throw new Error("dashboard.jsx no longer defines _baseOsLabel()");
let depth = 0, i = src.indexOf("{", start);
for (; i < src.length; i++) {
  if (src[i] === "{") depth++;
  else if (src[i] === "}") { depth--; if (depth === 0) break; }
}
const label = eval(`(${src.slice(start, i + 1)})`);

let failed = 0;
const check = (name, cond) => { if (!cond) { failed++; console.error(`FAIL: ${name}`); } };
check("emos reads emOS", label("emos") === "emOS");
check("fireos reads FireOS 5", label("fireos") === "FireOS 5");
check("null shows nothing", label(null) === null);
check("undefined shows nothing", label(undefined) === null);
check("an unknown value shows nothing, not a guess", label("linux") === null);

if (failed) { console.error(`${failed} check(s) failed.`); process.exit(1); }
console.log("base_os_label: all checks passed.");
