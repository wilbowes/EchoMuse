// Tests for the WiFi scan parser and security classifier in dashboard.jsx.
//
//     node controller/tests/wifi_scan.test.mjs
//
// The functions are lifted out of dashboard.jsx by source extraction rather
// than imported. That is not cleverness for its own sake: the dashboard is
// compiled with --bundle=false into a single classic script, so it cannot
// import a module, and the alternative is a second copy of this logic that
// drifts from the one that actually runs. If extraction fails the test fails
// loudly, which is the right outcome — it means something was renamed and
// this file needs to follow.
//
// What is under test decides whether a device can join a network at all, so
// getting it wrong costs someone an evening of "SCANNING" with no
// explanation (#82).

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

function liftConst(name) {
  const start = src.indexOf(`const ${name}`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}`);
  const end = src.indexOf("};", start);
  if (end < 0) throw new Error(`could not find the end of ${name}`);
  return src.slice(start, end + 2);
}

const code = [
  liftConst("_SECURITY_LABEL"),
  liftFunction("classifySecurity"),
  liftFunction("securityBlocker"),
  // The parser decodes SSIDs to their exact bytes (see wifi_ssid.test.mjs).
  liftFunction("_wpaUnescape"),
  liftFunction("_bytesHex"),
  liftFunction("_ssidText"),
  liftFunction("_ssidProblem"),
  liftFunction("parseScanResults"),
  "export { parseScanResults, classifySecurity, securityBlocker };",
].join("\n");

const { parseScanResults, classifySecurity, securityBlocker } = await import(
  "data:text/javascript;base64," + Buffer.from(code).toString("base64"));

// Real wpa_cli output shape: bssid \t frequency \t signal \t flags \t ssid
const RAW = [
  "bssid / frequency / signal level / flags / ssid",
  "aa:bb:cc:00:00:01\t2437\t-45\t[WPA2-PSK-CCMP][ESS]\tHomeNet",
  "aa:bb:cc:00:00:02\t5180\t-52\t[WPA2-PSK-CCMP][ESS]\tHomeNet",
  "aa:bb:cc:00:00:03\t5745\t-60\t[SAE-CCMP][ESS]\tPixel_7793",
  "aa:bb:cc:00:00:04\t2412\t-70\t[ESS]\tCoffeeShop",
  "aa:bb:cc:00:00:05\t2462\t-66\t[WPA2-EAP-CCMP][ESS]\tCorpWiFi",
  "aa:bb:cc:00:00:06\t2412\t-80\t[WPA2-PSK-CCMP][SAE-CCMP][ESS]\tMixedMode",
  "aa:bb:cc:00:00:07\t2437\t-75\t[WEP][ESS]\tOldRouter",
].join("\n");

let failures = 0;
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) {
    failures++;
    console.error(`FAIL  ${label}\n      got  ${JSON.stringify(got)}`
                + `\n      want ${JSON.stringify(want)}`);
  } else {
    console.log(`ok    ${label}`);
  }
}

const nets = parseScanResults(RAW);
const by = s => nets.find(n => n.ssid === s);

check("WPA2 network is joinable", by("HomeNet").blocker, null);
check("one SSID on two bands merges", by("HomeNet").bands.sort(), ["2.4GHz", "5GHz"]);
check("strongest AP wins the headline signal", by("HomeNet").signal, -45);

// The measured capability of this radio is NONE/IEEE8021X/WPA-EAP/WPA-PSK.
// No SAE, so a WPA3-only AP can never be joined and must be refused rather
// than attempted.
check("WPA3-only is classified", by("Pixel_7793").security, "wpa3");
check("WPA3-only is refused", typeof by("Pixel_7793").blocker, "string");

// The one that would be easy to get wrong in the other direction: a mixed
// WPA2/WPA3 AP advertises both, and is perfectly joinable through the WPA2
// half. Blocking it would lock people out of their own working network.
check("mixed WPA2/WPA3 uses the WPA2 half", by("MixedMode").security, "wpa2");
check("mixed WPA2/WPA3 is not refused", by("MixedMode").blocker, null);

check("open network is classified", by("CoffeeShop").security, "open");
check("open network is joinable", by("CoffeeShop").blocker, null);
check("enterprise is refused", typeof by("CorpWiFi").blocker, "string");
check("WEP is refused", typeof by("OldRouter").blocker, "string");

// One SSID served by a mixed WPA/WPA2 AP and a stronger WPA-only one, as in
// #1144: joinable through the mixed AP, so the WPA-only one must not lock
// the whole SSID out.
const SPLIT = [
  "aa:bb:cc:00:01:01\t2437\t-40\t[WPA-PSK-CCMP][ESS]\tHouse",
  "aa:bb:cc:00:01:02\t2462\t-70\t[WPA-PSK-CCMP+TKIP][WPA2-PSK-CCMP+TKIP][ESS]\tHouse",
  "aa:bb:cc:00:02:01\t2412\t-50\t[WPA-PSK-TKIP][ESS]\tOldOnly",
].join("\n");
const split = parseScanResults(SPLIT);
check("an SSID with any WPA2 AP is joinable", split.find(n => n.ssid === "House").blocker, null);
check("headline numbers stay the strongest AP's", split.find(n => n.ssid === "House").signal, -40);
check("a WPA-only SSID is classified", split.find(n => n.ssid === "OldOnly").security, "wpa1");
check("a WPA-only SSID is refused", typeof split.find(n => n.ssid === "OldOnly").blocker, "string");

// The cases em-wifi's security_of() is tested against (em-wifi-check.sh):
// the wizard and the console must agree on what this radio can join.
const AS_WIZARD = { psk: "wpa2", eap: "enterprise" };
const cases = readFileSync(join(HERE, "..", "..", "emos", "device", "wifi-security-cases.txt"), "utf8")
  .split("\n").filter(l => l && !l.startsWith("#"));
check("shared cases read", cases.length >= 30, true);
for (const line of cases) {
  const [cls, flags = ""] = line.split("\t");
  const want = AS_WIZARD[cls] || cls;
  check(`shared: ${flags || "(none)"} -> ${want}`, classifySecurity(flags), want);
  check(`shared: ${flags || "(none)"} ${want === "wpa2" || want === "open" ? "offered" : "refused"}`,
        securityBlocker(want) === null, want === "wpa2" || want === "open");
}

check("header row is skipped", nets.some(n => n.ssid === "ssid"), false);
check("sorted by signal", nets[0].ssid, "HomeNet");
check("label attached for the UI", by("Pixel_7793").securityLabel, "WPA3");
check("empty input", parseScanResults("").length, 0);
check("garbage input", parseScanResults("nonsense\nlines").length, 0);
check("null input", parseScanResults(null).length, 0);

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
