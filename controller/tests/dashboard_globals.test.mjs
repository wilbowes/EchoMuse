// Every name dashboard.jsx uses must be defined in it or be a browser global.
//
//     node controller/tests/dashboard_globals.test.mjs
//
// The dashboard is one classic script compiled in the image, so a name that
// is defined nowhere compiles cleanly and throws ReferenceError when its
// component first renders, which blanks the whole page. An undefined `mono`
// in the Bluetooth key panel did that on the dev add-on (2026-10-03).
//
// The image compiles the file with esbuild, which reports no such thing, so
// this parses it with @babel/standalone, whose scope analysis lists every
// reference with no binding. Babel is not in the repo: CI fetches 7.22.5,
// pinned by sha256, and passes its path as BABEL_STANDALONE. To run it here:
//
//     curl -sfLo /tmp/babel.min.js \
//       https://cdn.jsdelivr.net/npm/@babel/standalone@7.22.5/babel.min.js
//     BABEL_STANDALONE=/tmp/babel.min.js node controller/tests/dashboard_globals.test.mjs
//
// A new browser API has to be added to KNOWN, which is the point: the list
// is what the page depends on.

import { createRequire } from "node:module";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, "..", "static");
// A missing Babel fails rather than skips: a check that did not run is not
// a pass.
const BABEL = process.env.BABEL_STANDALONE || join(STATIC, "vendor", "babel.min.js");
assert.ok(existsSync(BABEL),
  `@babel/standalone not found at ${BABEL}; see the top of this file`);
const Babel = createRequire(import.meta.url)(BABEL);

const KNOWN = new Set(`
  AbortController Array Audio Blob Boolean DataView Date DecompressionStream
  Error FormData Int32Array JSON Map Math Number Object Promise React ReactDOM
  RegExp ResizeObserver Response Set String TextDecoder TextEncoder URL
  URLSearchParams Uint8Array WebSocket alert btoa clearInterval clearTimeout
  confirm console crypto document encodeURIComponent fetch localStorage
  location navigator parseInt setInterval setTimeout undefined unescape window
`.split(/\s+/).filter(Boolean));

function unbound(source) {
  let names = null;
  // Babel notes on stderr that it stops pretty-printing past 500KB.
  const note = console.error;
  console.error = () => {};
  try {
    Babel.transform(source, {
      presets: ["react"],
      plugins: [() => ({ visitor: { Program: { exit(p) {
        names = Object.keys(p.scope.globals);
      } } } })],
    });
  } finally {
    console.error = note;
  }
  return names.filter((n) => !KNOWN.has(n)).sort();
}

// The check itself: a name defined nowhere is reported, a local one is not.
assert.deepStrictEqual(
  unbound("const a = 1; function F() { return <b style={{font: mono}}>{a}</b>; }"),
  ["mono"]);

const missing = unbound(readFileSync(join(STATIC, "dashboard.jsx"), "utf8"));
assert.deepStrictEqual(missing, [],
  `dashboard.jsx uses names that are defined nowhere: ${missing.join(", ")}`);

console.log("dashboard_globals: all ok");
