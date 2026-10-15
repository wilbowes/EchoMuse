// The login page's single sign-on pieces that are pure enough to run.
//
//     node controller/tests/oidc_login.test.mjs
//
// The callback hands the browser a one-shot code in the URL FRAGMENT and a
// FIXED error code in the query. The page must read the first only from the
// fragment, and turn the second into words of its own: nothing in the URL is
// ever rendered, because the URL is attacker-influenced input.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import assert from "assert";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "index.html"), "utf8");

function liftFunction(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`index.html no longer defines ${name}()`);
  let depth = 0;
  for (let i = src.indexOf("{", start); i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`could not find the end of ${name}`);
}

const oidcCodeFromHash = new Function(`${liftFunction("oidcCodeFromHash")}; return oidcCodeFromHash;`)();
const oidcErrorMessage = new Function(`${liftFunction("oidcErrorMessage")}; return oidcErrorMessage;`)();

// ── the code comes from the fragment, and only a well-formed one ──
assert.strictEqual(oidcCodeFromHash("#oidc=abc-DEF_123"), "abc-DEF_123");
assert.strictEqual(oidcCodeFromHash(""), null);
assert.strictEqual(oidcCodeFromHash("#other=1"), null);
assert.strictEqual(oidcCodeFromHash("#oidc="), null);
assert.strictEqual(oidcCodeFromHash("#oidc=<script>"), null, "markup is not a code");
assert.strictEqual(oidcCodeFromHash("#oidc=a b"), null);
assert.strictEqual(oidcCodeFromHash("#oidc=" + "a".repeat(300)), null, "bounded length");

// ── error codes become our words; anything else becomes a generic line ──
for (const code of ["expired", "denied", "no_access", "failed"]) {
  const text = oidcErrorMessage(code);
  assert.ok(text && text.length > 10, `${code} has a sentence of its own`);
}
assert.notStrictEqual(oidcErrorMessage("no_access"), oidcErrorMessage("expired"));
const unknown = oidcErrorMessage("<img src=x onerror=alert(1)>");
assert.ok(!unknown.includes("<"), "an unknown code is never echoed");
assert.strictEqual(unknown, oidcErrorMessage("anything-else"));

console.log("oidc_login: ok");
