// Tests for the custom wake model list in dashboard.jsx (#905): the order the
// tiles appear in, what each says about its users, and how a refused delete
// reaches the operator.
//
//     node controller/tests/wake_models.test.mjs
//
// Source extraction rather than import, for the reason delete_ha_warning
// gives: the dashboard compiles to a single classic script with no module
// boundary, so the alternative is a second copy that drifts.
//
// The list is the only place a model in use can be told apart from one that is
// not. A delete the controller refuses must arrive as its own sentence — the
// refusal names the Echoes — because "Delete failed" leaves the operator with
// a model that will not go and no reason why.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import assert from "assert";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");

function liftFunction(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) {
    throw new Error(`dashboard.jsx no longer defines ${name}() — if it was `
                  + `renamed or moved, update this test to match`);
  }
  let depth = 0;
  for (let i = src.indexOf("{", start); i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) {
      return src.slice(start, i + 1);
    }
  }
  throw new Error(`could not find the end of ${name}()`);
}

const NAMES = ["sortWakeModels", "wakeModelUse", "wakeModelMeta", "_wakeModelError"];
const lifted = NAMES.map(liftFunction).join("\n");
const { sortWakeModels, wakeModelUse, wakeModelMeta, _wakeModelError } =
  await import("data:text/javascript;base64," + Buffer.from(
    lifted + `\nexport { ${NAMES.join(", ")} };`).toString("base64"));

let failures = 0;
function check(name, cond, detail) {
  if (cond) return;
  failures++;
  console.error(`FAIL: ${name}${detail ? `\n      ${detail}` : ""}`);
}

const NEVER = ["undefined", "null", "NaN", "[object Object]", "${"];

// ── Order: in use first, then by name ────────────────────────────────────────

const models = [
  { name: "a_leftover", used_by: [] },
  { name: "z_kitchen", used_by: ["Kitchen"] },
  { name: "m_fleet", used_by: ["Fleet default"] },
  { name: "n_pending" },
];
const order = sortWakeModels(models).map((m) => m.name);
check("models in use come before unused ones",
      order.slice(0, 2).every((n) => /kitchen|fleet/.test(n)), order);
check("within each group the list is by name",
      order.join(",") === "m_fleet,z_kitchen,a_leftover,n_pending", order);
check("a payload without used_by is not in use and does not throw",
      order.includes("n_pending"), order);
check("sorting does not mutate the list it was given",
      models[0].name === "a_leftover", models.map((m) => m.name).join(","));

// ── Who uses it ──────────────────────────────────────────────────────────────

check("the fleet default and an Echo are both named",
      wakeModelUse({ used_by: ["Fleet default", "Kitchen"] })
        === "used by Fleet default, Kitchen",
      wakeModelUse({ used_by: ["Fleet default", "Kitchen"] }));
check("an unused model says so, so the delete is not a guess",
      /not used/.test(wakeModelUse({ used_by: [] })));
check("a missing used_by reads as unused, not as a hole",
      !NEVER.some((bad) => wakeModelUse({}).includes(bad)), wakeModelUse({}));

// ── Upload time and size ────────────────────────────────────────────────────

const uploaded = wakeModelMeta({ size: 3072, mtime: Date.UTC(2026, 10, 5) / 1000 });
check("a small model is shown in KB", uploaded.includes("3 KB"), uploaded);
check("the upload date is shown", /2026/.test(uploaded) && /uploaded/.test(uploaded),
      uploaded);
check("a larger model is shown in MB",
      wakeModelMeta({ size: 3 * 1048576, mtime: 0 }).includes("3.0 MB"),
      wakeModelMeta({ size: 3 * 1048576, mtime: 0 }));
check("an entry with no size renders nothing rather than NaN",
      wakeModelMeta({}) === "" && !/NaN/.test(wakeModelMeta({ file: "x" })),
      wakeModelMeta({}));

// ── A refused delete reaches the operator ───────────────────────────────────

const refused = _wakeModelError(
  { code: "model_in_use", error: "Model is selected by: Kitchen" }, "Delete failed");
check("the controller's refusal is what is rendered",
      refused === "Model is selected by: Kitchen", refused);
check("a network error falls back to a sentence",
      _wakeModelError(new TypeError("fetch failed"), "Delete failed") === "Delete failed");
check("an error with no message falls back rather than showing undefined",
      _wakeModelError({ code: "model_limit" }, "Model upload failed")
        === "Model upload failed");

// ── It reaches the list and both call sites ─────────────────────────────────

check("the custom model tiles render the order the helper returns",
      src.includes("sortWakeModels(customModels)"));
check("each tile says who uses it", src.includes("wakeModelUse(m)"));
check("each tile says its size and upload time", src.includes("wakeModelMeta(m)"));
check("delete is offered only when nothing uses the model",
      /!disabled && !m\.missing && !m\.used_by\?\.length/.test(src));

const deleteBody = src.slice(src.indexOf("async function deleteWakeModel"));
check("the delete shows the refusal",
      deleteBody.slice(0, 600).includes(
        "_wakeModelError(e, 'Delete failed')"), deleteBody.slice(0, 600));
const uploadBody = src.slice(src.indexOf("async function uploadWakeModel"));
check("the upload shows the cap's reason",
      uploadBody.slice(0, 600).includes(
        "_wakeModelError(e, 'Model upload failed')"), uploadBody.slice(0, 600));

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("wake_models: all checks passed");
