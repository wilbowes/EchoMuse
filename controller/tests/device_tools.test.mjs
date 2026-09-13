// Tests for deviceTools() in dashboard.jsx — resolving which shell tools a
// recovery actually has, and the guard that no call site hardcodes busybox.
//
//     node controller/tests/device_tools.test.mjs
//
// Source extraction rather than import, for the reason boot_target.test.mjs
// gives: the dashboard compiles to one classic script with no module boundary.
//
// Why this exists: the wizard wrote `busybox md5sum` and `busybox dd`
// literally, and a stock FireOS 6 recovery has toybox and no busybox at all.
// Every such command exited 127 and produced an empty string, which the
// callers read as a hash mismatch — so a correctly installed start_server.sh
// was reported as "unreadable" and the run stopped. Measured 2026-09-13.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const JSX = join(HERE, "..", "static", "dashboard.jsx");
const src = readFileSync(JSX, "utf8");

// Keeps the `async` keyword: indexOf("function x") also matches inside
// "async function x", and lifting from there yields a sync function with an
// await in it, which does not parse.
function liftFunction(name) {
  let start = src.indexOf(`async function ${name}`);
  if (start < 0) start = src.indexOf(`function ${name}`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}()`);
  let depth = 0;
  let i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) break; }
  }
  return src.slice(start, i + 1);
}

const { deviceTools } = await import(
  "data:text/javascript;base64," + Buffer.from(
    "const _TOOL_NAMES = ['md5sum', 'dd', 'base64', 'tee'];\n"
    + liftFunction("deviceTools") + "\nexport { deviceTools };"
  ).toString("base64"));

let failures = 0;
function check(name, cond, detail) {
  if (cond) return;
  failures++;
  console.error(`FAIL: ${name}${detail ? `\n      ${detail}` : ""}`);
}

const fakeClient = (out) => ({ shell: async () => out });

// A stock FireOS 6 recovery: toybox, no busybox. The real measured case.
{
  const c = fakeClient(
    "TOOL md5sum md5sum\nTOOL dd dd\nTOOL base64 base64\nTOOL tee tee");
  const t = await deviceTools(c);
  check("toybox recovery resolves every tool plain",
        t.md5sum === "md5sum" && t.dd === "dd" && t.base64 === "base64" && t.tee === "tee",
        JSON.stringify(t));
}

// A recovery where busybox is what answers — a device with a root component
// added. Both forms have to keep working; this is not a migration away from
// busybox, it is not assuming either one.
{
  const c = fakeClient(
    "TOOL md5sum busybox md5sum\nTOOL dd busybox dd\n"
    + "TOOL base64 busybox base64\nTOOL tee busybox tee");
  const t = await deviceTools(c);
  check("busybox recovery keeps the busybox form",
        t.md5sum === "busybox md5sum" && t.dd === "busybox dd", JSON.stringify(t));
}

// Mixed, which is the case an "all or nothing" implementation would get wrong.
{
  const c = fakeClient(
    "TOOL md5sum md5sum\nTOOL dd busybox dd\nTOOL base64 base64\nTOOL tee tee");
  const t = await deviceTools(c);
  check("a mixed recovery resolves per tool",
        t.md5sum === "md5sum" && t.dd === "busybox dd", JSON.stringify(t));
}

// Resolved once per connection: the result is cached on the client, so the
// partition write does not pay a round trip per call.
{
  let calls = 0;
  const c = { shell: async () => { calls++; return "TOOL md5sum md5sum\nTOOL dd dd\nTOOL base64 base64\nTOOL tee tee"; } };
  await deviceTools(c);
  await deviceTools(c);
  check("tools are probed once per connection", calls === 1, `probed ${calls} times`);
}

// A tool that answers nowhere must THROW and name itself. Returning a guess
// would put an unverified write on a partition, and an empty string is what
// made the original bug read as corruption rather than as a missing tool.
for (const [what, out] of [
  ["nothing at all", ""],
  ["md5sum missing", "TOOL dd dd\nTOOL base64 base64\nTOOL tee tee"],
  ["dd missing", "TOOL md5sum md5sum\nTOOL base64 base64\nTOOL tee tee"],
]) {
  let threw = null;
  try { await deviceTools(fakeClient(out)); } catch (e) { threw = e; }
  check(`${what} throws`, threw !== null, "resolved instead of refusing");
  if (threw) {
    check(`${what} says nothing was written`,
          /nothing has been written/.test(threw.message), threw.message);
  }
}

// No call site may hardcode busybox again. Comments are stripped first — a
// guard that greps for the thing it forbids otherwise finds the comment
// explaining why it is forbidden and passes, which this repo has now done
// three times. LINE comments only: `/*/` inside the platform by-name glob is
// a complete block comment to that regex and would eat the rest of the file.
{
  const code = src
    .replace(/^[ \t]*\/\/.*$/gm, "")
    .replace(/^const _TOOL_NAMES[\s\S]*?^}/m, "");
  const hits = [...code.matchAll(/busybox (md5sum|dd|base64|tee)/g)].map(m => m[0]);
  check("no call site hardcodes a busybox tool",
        hits.length === 0,
        `found ${hits.join(", ")} — use deviceTools(c) instead`);
}

if (failures) {
  console.error(`\n${failures} check(s) failed.`);
  process.exit(1);
}
console.log("device_tools: all checks passed.");
