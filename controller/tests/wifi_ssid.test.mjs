// Tests for the SSID/passphrase helpers in dashboard.jsx: _wpaUnescape,
// _ssidProblem, _pskProblem and _wpaPsk — and that both wizard flows and the
// device WiFi panel use them.
//
//     node controller/tests/wifi_ssid.test.mjs
//
// Source extraction rather than import, for the reason pm_verdict.test.mjs
// gives. Every SSID the standard allows must work: the wizard used to refuse
// `"` and `\`, trim spaces, write `Caf\xc3\xa9` back as a different network,
// and on emOS put the SSID and password into a shell command, where an
// apostrophe broke it.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import { webcrypto } from "crypto";

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

function liftFunction(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}()`);
  const from = src.lastIndexOf("\n", start) + 1;   // keep a leading `async`
  let depth = 0;
  for (let i = src.indexOf("{", start); i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(from, i + 1);
  }
  throw new Error(`could not find the end of ${name}`);
}

const names = ["_wpaUnescape", "_bytesHex", "_hexBytes", "_ssidText",
               "_ssidProblem", "_pskProblem", "_confSsid", "_wpaPsk"];
const m = await import("data:text/javascript;base64," + Buffer.from(
  names.map(liftFunction).join("\n") + `\nexport { ${names.join(", ")} };`
).toString("base64"));

let failures = 0;
function check(name, cond, detail) {
  if (cond) return;
  failures++;
  console.error(`FAIL: ${name}${detail ? `\n      ${detail}` : ""}`);
}
const hex = s => m._bytesHex(m._wpaUnescape(s));
const enc = s => new TextEncoder().encode(s);
const utf8hex = s => m._bytesHex(new TextEncoder().encode(s));

// ── wpa_cli's printf_encode, reversed ──
for (const [escaped, real] of [
  ["My Home WiFi", "My Home WiFi"], [" padded ", " padded "], ["Bob's", "Bob's"],
  ['say \\"hi\\"', 'say "hi"'], ["back\\\\slash", "back\\slash"],
  ["Caf\\xc3\\xa9", "Café"], ["\\xf0\\x9f\\x8f\\xa0 home", "🏠 home"],
  ["tab\\there", "tab\there"], ["trailing\\", "trailing\\"], ["bad\\xZZ", "bad\\xZZ"],
]) {
  check(`unescape ${JSON.stringify(escaped)}`, hex(escaped) === utf8hex(real),
    `${hex(escaped)} vs ${utf8hex(real)}`);
}
check("non-UTF-8 bytes survive", hex("\\xff\\xfe") === "fffe");
check("invalid UTF-8 displays as U+FFFD",
  m._ssidText(Uint8Array.from([0x61, 0xff, 0x62])) === "a�b");
check("hex round trip", m._bytesHex(m._hexBytes("00ff7f")) === "00ff7f");

// ── limits ──
check("hidden (all zero) SSID refused", !!m._ssidProblem(Uint8Array.from([0, 0])));
check("empty SSID refused", !!m._ssidProblem(new Uint8Array(0)));
check("33-byte SSID refused", !!m._ssidProblem(new Uint8Array(33).fill(65)));
check("32-byte SSID allowed", m._ssidProblem(new Uint8Array(32).fill(65)) === null);
for (const p of ["", "12345678", "p".repeat(63), 'pa"ss\\word\'!', "has spaces", "AB".repeat(32)]) {
  check(`passphrase allowed: ${JSON.stringify(p)}`, m._pskProblem(p) === null);
}
for (const p of ["short", "p".repeat(64), "has\nnewline", "café-pass"]) {
  check(`passphrase refused: ${JSON.stringify(p)}`, !!m._pskProblem(p));
}

// ── the conf line: quoted when it can be, hex when not ──
for (const [name, want] of [
  ['Bob\'s "Home"', 'ssid="Bob\'s "Home""'], ["back\\slash", 'ssid="back\\slash"'],
  ["Café 🏠", 'ssid="Café 🏠"'], [" padded ", 'ssid=" padded "'],
  ["tab\there", "ssid=7461620968657265"],
]) {
  check(`conf line for ${JSON.stringify(name)}`, m._confSsid(enc(name)) === want, m._confSsid(enc(name)));
}
check("conf line for non-UTF-8 bytes is hex", m._confSsid(Uint8Array.from([0xff, 0xfe])) === "ssid=fffe");

// ── the PSK: IEEE 802.11i-2004 Annex H.4 test vectors ──
check("IEEE vector 1", await m._wpaPsk("password", enc("IEEE"))
  === "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e");
check("IEEE vector 2", await m._wpaPsk("ThisIsAPassword", enc("ThisIsASSID"))
  === "0dc0d6eb90555ed6419756b9a15ec3e3209b63df707dd508d14581f8982721af");
// Cross-checked against Python's hashlib.pbkdf2_hmac, 2026-09-19.
check("quotes and backslashes in both", await m._wpaPsk('pa"ss\\word', enc('Bob\'s "Home"'))
  === "4894d152ef2b45efecc3a9ddac662f237f82d7e86f33f0962eccb6a351634081");
check("a 64-hex input is already the PSK", await m._wpaPsk("AB".repeat(32), enc("x"))
  === "ab".repeat(32));

// ── the call sites ──
const slice = (from, len) => src.slice(src.indexOf(from), src.indexOf(from) + len);
const fire = slice("async function runConfigWifi", 12000);
check("FireOS flow writes the SSID through _confSsid", fire.includes("const ssidConf = _confSsid(ssidB)")
  && fire.includes("`\\t${ssidConf}`"));
check("FireOS flow verifies the line it wrote", (fire.match(/includes\(ssidConf\)/g) || []).length === 2);
const emos = slice("async function runEmosWifi", 3500);
check("emOS flow sends only hex to the console", /setNet\('ssid', ssidHex\)/.test(emos)
  && /setNet\('psk', pskHex\)/.test(emos));
check("emOS flow never interpolates the typed SSID or password into a command",
  !/set_network \$\{id\} (ssid|psk) '"\$\{wifi/.test(src));
check("emOS flow checks every set_network", /if \(!\/OK\/\.test\(r\)\)/.test(emos));
check("the old quote/backslash refusal is gone", !src.includes("function wpaConfEscape"));
check("the device panel forwards ssid_hex", src.includes("ssid_hex: seen.ssid_hex"));

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("wifi_ssid: all checks passed");
