// Tests for what the Status tab's Link row reads: _linkRow().
//
//     node controller/tests/link_row.test.mjs
//
// A TLS link is not a secure link. em_linkauth.decide rule 4 wants secure AND
// presented AND expected, so "every device shows wss (TLS)" is only a safe
// thing to check before flipping REQUIRE_DEVICE_TLS if the row also says
// whether a token is on record. Before this, a device with nothing on record
// read green — and one did, on the EA controller 2026-09-20, logging "token
// presented but none on record" on every plane on every dial.
//
// Absence of the field is not a measurement: a dashboard served by a
// controller that has never heard of linkTokenIssued must keep today's
// reading rather than claim "no token" about every device at once.

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
const linkRow = lift("_linkRow");

let failed = 0;
const check = (name, cond) => { if (!cond) { failed++; console.error(`FAIL: ${name}`); } };
const dev = (o) => ({ connected: true, linkTls: true, linkTokenIssued: true, ...o });

const tlsToken   = linkRow(dev());
check("TLS with a token reads wss (TLS)", tlsToken.label === "wss (TLS)");
check("TLS with a token is ok", tlsToken.color === "var(--ok)");

const tlsNoToken = linkRow(dev({ linkTokenIssued: false }));
check("TLS with no token says so",
      tlsNoToken.label === "wss (TLS) · no token");
check("TLS with no token is NOT ok", tlsNoToken.color !== "var(--ok)");
check("TLS with no token is amber", tlsNoToken.color === "var(--warn)");

const wsToken = linkRow(dev({ linkTls: false }));
check("plain with a token still reads plain ws", wsToken.label === "plain ws");
check("plain with a token is still warn", wsToken.color === "var(--warn)");

const wsNoToken = linkRow(dev({ linkTls: false, linkTokenIssued: false }));
check("plain with no token reads plain ws", wsNoToken.label === "plain ws");
check("plain with no token is warn", wsNoToken.color === "var(--warn)");

const refused = linkRow(dev({ connected: false, linkRefused: { reason: "token mismatch" } }));
check("a refusal names the reason", refused.label === "Refused: token mismatch");
check("a refusal is red", refused.color === "var(--error)");
check("a refusal is flagged so the row can wrap it", refused.refused === true);

const away = linkRow({ connected: false });
check("offline with nothing says nothing", away.label === "—");
check("offline with nothing has no colour", away.color === undefined);
check("offline is not flagged as refused", !away.refused);

// An older controller's /api/devices has never heard of the field. Absence
// must not read as a measurement, or every device would say "no token".
const unknown = linkRow({ connected: true, linkTls: true });
check("an absent field keeps the TLS reading", unknown.label === "wss (TLS)");
check("an absent field keeps the ok colour", unknown.color === "var(--ok)");

if (failed) { console.error(`${failed} check(s) failed.`); process.exit(1); }
console.log("link_row: all checks passed.");