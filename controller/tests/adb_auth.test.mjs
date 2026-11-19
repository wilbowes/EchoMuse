// Tests for the wizard's ADB connect (_ADB.Client.requestDevice in
// dashboard.jsx): the key store it hands the library, the versions it loads,
// and what happens when the Echo never accepts the browser's key.
//
//     node controller/tests/adb_auth.test.mjs
//
// Source extraction rather than import, as the other dashboard tests do. The
// esm.sh imports are swapped for stubs that keep @yume-chan/adb 2.1.0's
// contract: AdbDaemonTransport.authenticate runs the default authenticators
// over options.credentialStore (esm/daemon/auth.js: credentialStore
// .iterateKeys()) whenever adbd answers CNXN with AUTH, and waits for ever
// when adbd does not accept the key it is sent.
//
// #1206: the store was never passed. TWRP never asks for a key, so this
// passed every bench run; FireOS with ro.adb.secure=1 (firebreak since
// 2026-11-11) crashed step 1 on iterateKeys of undefined.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

function liftIife(head) {
  const start = src.indexOf(head);
  if (start < 0) throw new Error(`dashboard.jsx no longer has ${head}`);
  let depth = 0, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) break; }
  }
  const tail = src.indexOf("})();", i);
  if (tail !== i) throw new Error(`${head} no longer ends in })();`);
  return src.slice(start, tail + 5);
}

let fails = 0;
function check(what, cond) {
  if (!cond) { console.error(`FAIL ${what}`); fails++; }
  else console.log(`ok   ${what}`);
}

// ── The loader's URLs ──

const urls = [...src.matchAll(/import\('(https:\/\/esm\.sh\/@yume-chan\/[^']+)'\)/g)].map(m => m[1]);
check("the ADB loader imports three @yume-chan modules", urls.length === 3);
check("the credential store is one of them",
  urls.some(u => u.includes("/@yume-chan/adb-credential-web@")));
for (const u of urls) {
  const m = u.match(/@yume-chan\/([a-z-]+)@([0-9.]+)/);
  check(`${m[1]} is pinned to an exact version`, !!m && /^\d+\.\d+\.\d+$/.test(m[2]));
  // Each one that depends on adb must bundle the SAME adb: credential-web
  // declares ^2.1.0, and unpinned it resolves to 2.6.x beside ours.
  if (m[1] !== "adb") check(`${m[1]} pins its adb to 2.1.0`, u.includes("deps=@yume-chan/adb@2.1.0"));
}

// ── requestDevice against stubbed modules ──

const scenario = { asksForKey: true, acceptsKey: true, seen: null };

class FakeStore { constructor(name) { this.name = name; }
  async *iterateKeys() { yield { buffer: new Uint8Array(4), name: this.name }; } }

const MODS = {
  webusb: { AdbDaemonWebUsbDeviceManager: { BROWSER: {
    requestDevice: async () => ({ serial: "G090LF0000001234", name: "AEOBC",
                                  connect: async () => ({}), disconnect: async () => {} }),
  } } },
  adb: {
    ADB_DEFAULT_AUTHENTICATORS: ["sig", "pub"],
    Adb: class { constructor(t) { this.banner = { product: "biscuit" }; } },
    AdbDaemonTransport: { async authenticate(opts) {
      scenario.seen = opts;
      if (scenario.asksForKey) {
        // adb 2.1.0 auth.js, AdbSignatureAuthenticator.
        for await (const _ of opts.credentialStore.iterateKeys()) break;
        if (!scenario.acceptsKey) await new Promise(() => {});
      }
      return { close: async () => {} };
    } },
  },
  cred: { default: FakeStore },
};
function stubImport(url) {
  if (url.includes("adb-credential-web")) return Promise.resolve(MODS.cred);
  if (url.includes("adb-daemon-webusb")) return Promise.resolve(MODS.webusb);
  return Promise.resolve(MODS.adb);
}

const body = liftIife("const _ADB = (() => {").replace(/\bimport\(/g, "stubImport(");
const _ADB = new Function("stubImport", "webUsbBlocked", "ADB_AUTH_TIMEOUT_MS",
                          `${body}\nreturn _ADB;`)(stubImport, () => null, 50);

const log = [];
const logFn = l => log.push(l);

// TWRP: no AUTH, connects with or without a store.
scenario.asksForKey = false;
let client = await _ADB.Client.requestDevice(logFn);
check("TWRP (no key asked) connects", client.banner === "biscuit");

// FireOS, key already trusted: the store must be there and be the web one.
scenario.asksForKey = true; scenario.acceptsKey = true;
client = await _ADB.Client.requestDevice(logFn);
check("secure adbd with a known key connects", client.banner === "biscuit");
check("authenticate is given a credential store",
  scenario.seen.credentialStore instanceof FakeStore);
check("the store keeps the browser key under one name",
  scenario.seen.credentialStore.name === "EchoMuse");

// FireOS that does not know the key: a bounded wait and a reason, not a hang.
scenario.acceptsKey = false;
let err = null;
const t0 = Date.now();
// The watchdog keeps the event loop alive: without it a hang ends with node
// exiting silently on an unsettled promise rather than a FAIL.
let dog;
try {
  await Promise.race([
    _ADB.Client.requestDevice(logFn),
    new Promise(r => { dog = setTimeout(() => r("hung"), 1500); }),
  ]);
} catch (e) { err = e; }
clearTimeout(dog);
check("an unaccepted key fails rather than hanging", err instanceof Error);
check("it fails at the timeout", Date.now() - t0 < 2000);
check("the reason sends the user to TWRP", /TWRP/.test(err?.message ?? ""));
check("it never claims to have authenticated",
  log.filter(l => l === "ADB authenticated.").length === 2);

if (fails) { console.error(`${fails} failed`); process.exit(1); }
console.log("all passed");
