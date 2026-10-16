// Tests for the cloud-providers row helpers in dashboard.jsx: what counts as
// a touched row, and what a row's PATCH entry carries.
//
//     node controller/tests/providers_ui.test.mjs
//
// Source extraction rather than import, for the reason slot_choice.test.mjs
// gives: the dashboard compiles to one classic script with no module
// boundary, so the alternative is a second copy that drifts.
//
// The rules under test are the write-only key contract (GET shows key_set,
// never the key; PATCH sends plaintext-new, the sentinel, or "" for remove):
// an untouched row must send the sentinel, and opening the key setter without
// typing must not read as a removal.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

// The sentinel the helpers close over. Pinned to the backend's value rather
// than read out of the file, so a rename breaks loudly here instead of
// silently sending a string the server stores as a new key.
const PROVIDER_SENTINEL = "__unchanged__";
if (!src.includes("const PROVIDER_SENTINEL = '__unchanged__'")) {
  throw new Error("dashboard.jsx no longer defines PROVIDER_SENTINEL as '__unchanged__' — " +
    "the PATCH contract in em_provider_keys.py depends on that exact string");
}

function liftFunction(name) {
  const start = src.indexOf(`function ${name}`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}()`);
  let depth = 0, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) break; }
  }
  return src.slice(start, i + 1);
}

const providerRowDirty = eval(`(${liftFunction("providerRowDirty")})`);
const providerPatchRow = eval(`(${liftFunction("providerPatchRow")})`);

let fails = 0;
function check(what, cond) {
  if (cond) { console.log(`ok    ${what}`); }
  else { console.log(`FAIL  ${what}`); fails++; }
}

const SET = { id: "openai", key_set: true, default_model: "gpt-4o", models: ["gpt-4o", "o3"] };
const UNSET = { id: "groq", key_set: false, default_model: "llama-3", models: ["llama-3"] };

// ── what counts as touched ───────────────────────────────────────────────────
check("untouched set row is clean", providerRowDirty(SET) === false);
check("untouched unset row is clean", providerRowDirty(UNSET) === false);
check("sentinel key is clean", providerRowDirty(SET, "__unchanged__") === false);
check("new key is dirty", providerRowDirty(SET, "new-key") === true);
check("new key on a keyless provider is dirty", providerRowDirty(UNSET, "new-key") === true);
check("empty key on a set provider is a removal: dirty", providerRowDirty(SET, "") === true);
check("empty key on a keyless provider is nothing typed yet: clean",
  providerRowDirty(UNSET, "") === false);
check("changed model is dirty", providerRowDirty(SET, undefined, "o3") === true);
check("same model is clean", providerRowDirty(SET, undefined, "gpt-4o") === false);

// ── what a row's PATCH entry carries ─────────────────────────────────────────
{
  const e = providerPatchRow(SET);
  check("untouched row sends the sentinel key",
    e.id === "openai" && e.key === "__unchanged__" && e.default_model === "gpt-4o");
}
{
  const e = providerPatchRow(SET, "new-key", "o3");
  check("edited row sends plaintext key and chosen model",
    e.key === "new-key" && e.default_model === "o3");
}
{
  const e = providerPatchRow(SET, "");
  check("empty edit on a set provider sends removal", e.key === "");
}
{
  const e = providerPatchRow(UNSET, "");
  check("empty edit on a keyless provider normalises to the sentinel",
    e.key === "__unchanged__");
}
{
  const e = providerPatchRow(UNSET, undefined, "llama-3");
  check("model-only edit keeps the sentinel key",
    e.key === "__unchanged__" && e.default_model === "llama-3");
}

// ── the screen calls the contracted endpoints ───────────────────────────────
check("panel GETs the provider list",
  src.includes("API.get('/api/system/providers')"));
check("save PATCHes the provider list",
  src.includes("API.patch('/api/system/providers'"));
check("test POSTs one provider id",
  src.includes("API.post('/api/system/providers/test'"));
{
  const row = src.slice(src.indexOf("function ProviderRow"));
  check("the key field reuses PasswordField", row.includes("<PasswordField"));
}

// ── ask box: one-shot probe of the saved key ─────────────────────────────────
check("ask POSTs id and prompt to the ask endpoint",
  src.includes("API.post('/api/system/providers/ask', { id: p.id, prompt })"));
{
  const at = src.indexOf("'/api/system/providers/ask'");
  check("ask call site is inside ProviderRow",
    at > src.indexOf("function ProviderRow") && at < src.indexOf("function CloudProvidersPanel"));
  // The prompt travels inline in the POST body, not via a named model field:
  // the row sends no model override, so the saved default applies server-side.
  const call = src.slice(at, at + 120);
  check("ask payload carries no model override", !call.includes("model"));
}
{
  const row = src.slice(
    src.indexOf("function ProviderRow"), src.indexOf("function CloudProvidersPanel"));
  check("ask renders the answer through testMsg",
    row.includes("setTestMsg({ ok: true, text: r.answer })"));
  check("ask renders the failure through testMsg",
    row.includes("setTestMsg({ ok: false, text: e.error || 'Ask failed' })"));
  check("ask sends only on a non-blank prompt",
    row.includes("!prompt.trim()"));
  // The prompt and answer live only in component state: no storage write,
  // no console echo, no key material in the ask path.
  for (const banned of ["localStorage", "sessionStorage", "console.log", "console.debug"]) {
    check(`ask path never touches ${banned}`, !row.includes(banned));
  }
  const askAt = row.indexOf("'/api/system/providers/ask'");
  check("ask payload never carries key material",
    !row.slice(askAt, askAt + 120).includes("key"));
}

// ── controller-only: structurally separate from device config ───────────────
{
  const m = src.match(/const CONFIG_SECTIONS\s*=\s*(\{.*?\n\});/s);
  check("CONFIG_SECTIONS literal still parses", !!m);
  const js = JSON.parse(m[1]);
  const ids = ["gemini", "groq", "openai", "claude", "deepseek", "nvidia",
               "openrouter", "agentrouter", "fluxion", "opencode_zen"];
  const leaked = Object.values(js).flat().filter(k => ids.includes(k));
  check("no provider id leaks into CONFIG_SECTIONS", leaked.length === 0);
}
check("providers panel is module scope, not nested in SettingsPanel",
  src.indexOf("function CloudProvidersPanel") < src.indexOf("function SettingsPanel"));

console.log(fails ? "\nFAILED" : "\nall ok");
process.exit(fails ? 1 : 0);
