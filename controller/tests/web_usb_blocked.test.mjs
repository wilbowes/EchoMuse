// Tests for the WebUSB pre-flight check in dashboard.jsx.
//
//     node controller/tests/web_usb_blocked.test.mjs
//
// Same source-extraction approach as wizard_log_class.test.mjs: the functions
// are lifted out of dashboard.jsx rather than imported, because the dashboard
// compiles to a single classic script. If either is renamed or moved, the lift
// fails loudly and this file must follow.
//
// What matters here is the ADVICE, not the detection. WebUSB needs a secure
// context and nothing in this repo changes that, so the only job these strings
// have is telling somebody what to do — and getting that wrong sends them
// somewhere that cannot work:
//
//   - Under the add-on, `localhost:8768` is a 403. _ingress_only_middleware
//     rejects anything that is not the Supervisor gateway, so offering a
//     direct port there is worse than offering nothing.
//   - Chrome's insecure-origin allowlist matches scheme, host AND port
//     exactly, and the add-on's page is served from Home Assistant's origin
//     rather than the controller's. Naming the wrong origin, or not naming one
//     at all, is what #169 was filed for.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const JSX = join(HERE, "..", "static", "dashboard.jsx");
const src = readFileSync(JSX, "utf8");

function liftFunction(name) {
  const start = src.indexOf(`function ${name}`);
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

// Both are lifted: webUsbBlocked calls isIngress, so stubbing isIngress here
// would test a copy of the branch rather than the branch.
//
// The browser globals are declared at MODULE scope so the lifted functions
// resolve them lexically, rather than being assigned onto globalThis. Node 21
// added a real `navigator` global with only a getter, so assigning to it
// throws — which passes on Node 20 and fails in CI. Shadowing needs no
// permission from the runtime and cannot rot the same way.
const code = [
  "let navigator, window, document;",
  "export function __setPage(n, w, d) { navigator = n; window = w; document = d; }",
  liftFunction("isIngress"),
  liftFunction("webUsbBlocked"),
  "export { webUsbBlocked };",
].join("\n");

const { webUsbBlocked, __setPage } = await import(
  "data:text/javascript;base64," + Buffer.from(code).toString("base64"));

let failures = 0;
function check(label, cond, detail) {
  if (cond) { console.log(`ok    ${label}`); return; }
  failures++;
  console.error(`FAIL  ${label}${detail ? `\n      ${detail}` : ""}`);
}

// The globals the two functions read. Set per-case rather than once, since the
// whole point is that the answer differs by deployment.
function withPage({ usb, origin, baseURI }, fn) {
  __setPage(usb ? { usb: {} } : {}, { location: { origin } }, { baseURI });
  try { return fn(); } finally { __setPage(undefined, undefined, undefined); }
}

const STANDALONE = { origin: "http://192.168.1.10:8768", baseURI: "http://192.168.1.10:8768/" };
const INGRESS = {
  origin: "http://homeassistant.local:8123",
  baseURI: "http://homeassistant.local:8123/api/hassio_ingress/abc123/",
};

// A working browser must produce nothing at all — this gates a panel and a
// disabled button, so a false positive blocks provisioning outright.
check("secure context returns null (standalone)",
      withPage({ ...STANDALONE, usb: true }, () => webUsbBlocked()) === null);
check("secure context returns null (ingress)",
      withPage({ ...INGRESS, usb: true }, () => webUsbBlocked()) === null);

// Standalone: localhost is a real route, so it is offered.
{
  const b = withPage({ ...STANDALONE, usb: false }, () => webUsbBlocked());
  check("standalone reports a blocker", b !== null);
  check("standalone names its own origin", b.why.includes(STANDALONE.origin), b.why);
  check("standalone offers localhost", b.fix.includes("http://localhost:8768"), b.fix);
  check("standalone names the origin in the flag advice",
        b.fix.includes(STANDALONE.origin), b.fix);
}

// Ingress: localhost is a 403 there, so it must NOT be offered. This is the
// check worth having — the advice being actively wrong is how #169 started.
{
  const b = withPage({ ...INGRESS, usb: false }, () => webUsbBlocked());
  check("ingress reports a blocker", b !== null);
  check("ingress does NOT offer localhost", !b.fix.includes("localhost"), b.fix);
  check("ingress offers HTTPS on Home Assistant", /HTTPS/.test(b.fix), b.fix);
  check("ingress names HOME ASSISTANT's origin, not the controller's",
        b.fix.includes(INGRESS.origin) && !b.fix.includes("8768"), b.fix);
  check("ingress warns an existing controller allowlist entry does not cover it",
        /does not cover/.test(b.fix), b.fix);
}

// Both deployments must name the flag exactly; a paraphrase is not actionable.
for (const [name, page] of [["standalone", STANDALONE], ["ingress", INGRESS]]) {
  const b = withPage({ ...page, usb: false }, () => webUsbBlocked());
  check(`${name} names the chrome flag exactly`,
        b.fix.includes("chrome://flags/#unsafely-treat-insecure-origin-as-secure"), b.fix);
  check(`${name} says to relaunch the browser`, /relaunch/.test(b.fix), b.fix);
}

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
