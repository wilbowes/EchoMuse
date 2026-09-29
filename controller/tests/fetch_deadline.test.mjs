// Tests for fetchWithDeadline() in dashboard.jsx — the deadline on the emOS
// build POST (#689), which used to wait forever on a request that never came
// back.
//
//     node controller/tests/fetch_deadline.test.mjs
//
// Source extraction rather than import, for the reason wifi_scan.test.mjs
// gives. Runs against real local servers, since the point is how fetch
// behaves when the other end goes quiet.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import http from "http";
import assert from "assert/strict";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

function liftFunction(name) {
  const start = src.indexOf(`async function ${name}`);
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

const fetchWithDeadline = new Function(
  `${liftFunction("fetchWithDeadline")}; return fetchWithDeadline;`)();

function serve(handler) {
  return new Promise(res => {
    const srv = http.createServer(handler);
    srv.listen(0, "127.0.0.1", () => res(srv));
  });
}
const url = srv => `http://127.0.0.1:${srv.address().port}/`;

// Silent: takes the upload and never answers.
{
  const srv = await serve((req) => { req.resume(); });
  const t0 = Date.now();
  await assert.rejects(
    fetchWithDeadline(url(srv), { method: "POST", body: "x".repeat(1 << 20) }, 300),
    e => e.name === "TimeoutError");
  assert.ok(Date.now() - t0 < 3000, "the deadline did not end the request");
  srv.closeAllConnections(); srv.close();
}

// Headers sent, body stalled: the deadline covers the body too.
{
  const srv = await serve((req, res) => { res.writeHead(200); res.write("part"); });
  await assert.rejects(fetchWithDeadline(url(srv), {}, 300),
                       e => e.name === "TimeoutError");
  srv.closeAllConnections(); srv.close();
}

// An answer in time: status, headers and body come through intact.
{
  const srv = await serve((req, res) => {
    res.writeHead(422, { "X-Image-MD5": "abc", "Content-Type": "application/json" });
    res.end('{"message":"refused"}');
  });
  const r = await fetchWithDeadline(url(srv), { method: "POST", body: "y" }, 2000);
  assert.equal(r.status, 422);
  assert.equal(r.ok, false);
  assert.equal(r.headers.get("X-Image-MD5"), "abc");
  assert.deepEqual(await r.json(), { message: "refused" });
  srv.closeAllConnections(); srv.close();
}

// A refused connection is not a timeout.
{
  const srv = await serve(() => {});
  const dead = url(srv); srv.close();
  await assert.rejects(fetchWithDeadline(dead, {}, 2000), e => e.name !== "TimeoutError");
}

console.log("fetch_deadline: ok");
