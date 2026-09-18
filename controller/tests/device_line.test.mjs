// Tests for what a device's tile and detail header print: _baseOsLabel() (the
// emOS / FireOS 5 slug) and _middleEllipsis() (long labels and versions).
//
//     node controller/tests/device_line.test.mjs
//
// Null must show NOTHING: old firmware cannot report its base, and a device
// that has never registered has none on record. Guessing "FireOS" there is the
// wrong answer the compatibility rules exist to prevent.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");
function lift(name) {
  const start = src.indexOf(`function ${name}`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}()`);
  let depth = 0, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) break; }
  }
  return eval(`(${src.slice(start, i + 1)})`);
}
const label = lift("_baseOsLabel");
const mid = lift("_middleEllipsis");

let failed = 0;
const check = (name, cond) => { if (!cond) { failed++; console.error(`FAIL: ${name}`); } };
check("emos reads emOS", label("emos") === "emOS");
check("fireos reads FireOS 5", label("fireos") === "FireOS 5");
check("null shows nothing", label(null) === null);
check("undefined shows nothing", label(undefined) === null);
check("an unknown value shows nothing, not a guess", label("linux") === null);

// Two labels that differ only at the end must still differ once shortened.
const a = mid("this is my test device numbered 01", 22);
const b = mid("this is my test device numbered 02", 22);
check("long labels are cut to the budget", a.length === 22 && b.length === 22);
check("labels differing at the end stay different", a !== b && a.endsWith("01") && b.endsWith("02"));
check("the start survives", a.startsWith("this is"));
check("short text is untouched", mid("Kitchen", 22) === "Kitchen");
check("a version keeps its number and build hash",
      mid("v2.15.0-37-gd5e9456", 16, 7) === "v2.15.0-…d5e9456");
check("empty and missing text pass through", mid("", 10) === "" && mid(undefined, 10) === undefined);
check("a tail longer than the budget cannot eat the start", mid("abcdefghijklmnop", 6, 20).startsWith("a"));

if (failed) { console.error(`${failed} check(s) failed.`); process.exit(1); }
console.log("device_line: all checks passed.");
