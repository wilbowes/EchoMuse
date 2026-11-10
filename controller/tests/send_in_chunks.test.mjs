// Tests for _sendInChunks() in dashboard.jsx — the wizard's boot image sent
// in pieces small enough for any proxy (#800).
//
//     node controller/tests/send_in_chunks.test.mjs
//
// Source extraction rather than import, for the reason wifi_scan.test.mjs
// gives. nginx refuses a body over 1MB by default, and #769 and #1001 both
// stopped at the one ~9MB POST; tests/test_chunked.py holds the other end.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

function liftFunction(name) {
  const start = src.indexOf(`async function ${name}`);
  if (start < 0) {
    throw new Error(`dashboard.jsx no longer defines ${name}() — if it was `
                  + `renamed or moved, update this test to match`);
  }
  let depth = 0;
  let i = src.indexOf("{", src.indexOf(")", start));
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) break; }
  }
  return src.slice(start, i + 1);
}

const { _sendInChunks } = await import(
  "data:text/javascript;base64," + Buffer.from(
    liftFunction("_sendInChunks") + "\nexport { _sendInChunks };"
  ).toString("base64"));

let failures = 0;
function check(what, ok, detail = "") {
  if (ok) { console.log(`ok    ${what}`); return; }
  failures++;
  console.error(`FAIL: ${what}${detail ? `\n      ${detail}` : ""}`);
}

const ok = { ok: true, status: 200, json: async () => ({}) };

// A 9MB image, the size #1001 sent, not a multiple of the piece size.
const image = new Uint8Array(9 * 1024 * 1024 + 123);
for (let i = 0; i < image.length; i++) image[i] = (i * 31) & 0xff;

{
  const sent = [];
  const id = await _sendInChunks(image, async (q, piece) => {
    sent.push({ q: new URLSearchParams(q.slice(1)), piece: piece.slice() });
    return ok;
  });
  const largest = Math.max(...sent.map(s => s.piece.length));
  check("every piece is under nginx's 1MB default", largest <= 512 * 1024, `largest ${largest}`);
  const joined = new Uint8Array(image.length);
  let at = 0, inOrder = true;
  for (const s of sent) {
    inOrder &&= Number(s.q.get("offset")) === at;
    joined.set(s.piece, at); at += s.piece.length;
  }
  check("offsets run in order with no gap", inOrder && at === image.length);
  check("the pieces reassemble to the image", joined.every((b, i) => b === image[i]));
  check("every piece names the same id and total",
        sent.every(s => s.q.get("id") === id && Number(s.q.get("total")) === image.length));
  check("the id is 32 hex characters", /^[0-9a-f]{32}$/.test(id), id);
}

{
  // A network failure on one piece is retried with the same bytes.
  let calls = 0, failedOnce = false;
  await _sendInChunks(image.subarray(0, 1024), async () => {
    calls++;
    if (!failedOnce) { failedOnce = true; throw new TypeError("Failed to fetch"); }
    return ok;
  });
  check("a piece that fails on the network is sent again", calls === 2);
}

{
  let err = null;
  try {
    await _sendInChunks(image.subarray(0, 1024), async () => ({ ok: false, status: 413 }));
  } catch (e) { err = e; }
  check("a 413 names the proxy's limit", err && /client_max_body_size/.test(err.message)
        && /Nothing has been written/.test(err.message), err && err.message);
}

{
  let err = null;
  try {
    await _sendInChunks(image.subarray(0, 1024), async () => ({
      ok: false, status: 400, json: async () => ({ message: "chunk runs past the declared total" }) }));
  } catch (e) { err = e; }
  check("a refusal carries the controller's reason",
        err && /runs past the declared total/.test(err.message), err && err.message);
}

if (failures) { console.error(`send_in_chunks: ${failures} failed`); process.exit(1); }
console.log("send_in_chunks: all checks passed.");
