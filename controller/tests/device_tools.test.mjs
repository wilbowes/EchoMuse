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

// _TOOL_NAMES is lifted from the source too, never restated here. A second
// copy of the list silently disagrees the moment a tool is added — which it
// did, the first time `sync` was added to the real one.
function liftConst(name) {
  const m = src.match(new RegExp(`^const ${name} = .*?;$`, "m"));
  if (!m) throw new Error(`dashboard.jsx no longer defines ${name}`);
  return m[0];
}

const { deviceTools, _TOOL_NAMES } = await import(
  "data:text/javascript;base64," + Buffer.from(
    liftConst("_TOOL_NAMES") + "\n"
    + liftFunction("deviceTools") + "\nexport { deviceTools, _TOOL_NAMES };"
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
    "TOOL md5sum md5sum\nTOOL dd dd\nTOOL base64 base64\nTOOL tee tee\nTOOL sync sync");
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
    + "TOOL base64 busybox base64\nTOOL tee busybox tee\nTOOL sync sync");
  const t = await deviceTools(c);
  check("busybox recovery keeps the busybox form",
        t.md5sum === "busybox md5sum" && t.dd === "busybox dd", JSON.stringify(t));
}

// Mixed, which is the case an "all or nothing" implementation would get wrong.
{
  const c = fakeClient(
    "TOOL md5sum md5sum\nTOOL dd busybox dd\nTOOL base64 base64\nTOOL tee tee\nTOOL sync sync");
  const t = await deviceTools(c);
  check("a mixed recovery resolves per tool",
        t.md5sum === "md5sum" && t.dd === "busybox dd", JSON.stringify(t));
}

// Resolved once per connection: the result is cached on the client, so the
// partition write does not pay round trips per call. Resolution itself takes
// more than one shell call (the tools, then whether dd accepts conv=), so what
// is asserted is that a SECOND call spends nothing — not a call count.
{
  let calls = 0;
  const c = { shell: async (cmd) => {
    calls++;
    return cmd.includes("CONV_OK") ? "CONV_OK"
         : "TOOL md5sum md5sum\nTOOL dd dd\nTOOL base64 base64\nTOOL tee tee\nTOOL sync sync";
  } };
  await deviceTools(c);
  const afterFirst = calls;
  await deviceTools(c);
  check("a second resolve costs nothing", calls === afterFirst,
        `${calls - afterFirst} extra shell call(s) on the cached path`);
  check("the first resolve is bounded", afterFirst <= 2, `${afterFirst} calls to resolve`);
}

// A tool that answers nowhere must THROW and name itself. Returning a guess
// would put an unverified write on a partition, and an empty string is what
// made the original bug read as corruption rather than as a missing tool.
for (const [what, out] of [
  ["nothing at all", ""],
  ["md5sum missing", "TOOL dd dd\nTOOL base64 base64\nTOOL tee tee\nTOOL sync sync"],
  ["dd missing", "TOOL md5sum md5sum\nTOOL base64 base64\nTOOL tee tee\nTOOL sync sync"],
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

// ── conv=fsync ───────────────────────────────────────────────────────────────
//
// toybox builds conv= optionally and a stock FireOS 6 recovery has it compiled
// out: `dd ... conv=fsync` answers "dd: conv option disabled" and copies
// nothing. On a partition write that presents as impossible throughput and a
// read-back still holding the old image, which is what it cost on hardware.
{
  const c = {
    shell: async (cmd) => cmd.includes("CONV_OK")
      ? "CONV_OK"
      : "TOOL md5sum md5sum\nTOOL dd dd\nTOOL base64 base64\nTOOL tee tee\nTOOL sync sync",
  };
  const t = await deviceTools(c);
  check("conv=fsync is used where dd supports it", t.ddConv === " conv=fsync", JSON.stringify(t));
}

{
  const c = {
    shell: async (cmd) => cmd.includes("CONV_OK")
      ? "dd: conv option disabled"
      : "TOOL md5sum md5sum\nTOOL dd dd\nTOOL base64 base64\nTOOL tee tee\nTOOL sync sync",
  };
  const t = await deviceTools(c);
  check("conv=fsync is dropped where dd refuses it", t.ddConv === "", JSON.stringify(t));
  check("the dd tool itself still resolves", t.dd === "dd", JSON.stringify(t));
}

// The flag is a whole argument including its leading space, so composing it
// into the command can never produce `bs=1048576conv=fsync`.
{
  const c = {
    shell: async (cmd) => cmd.includes("CONV_OK")
      ? "CONV_OK"
      : "TOOL md5sum md5sum\nTOOL dd dd\nTOOL base64 base64\nTOOL tee tee\nTOOL sync sync",
  };
  const t = await deviceTools(c);
  check("the conv flag carries its own separator", t.ddConv.startsWith(" "), JSON.stringify(t.ddConv));
}

// _writeBootPartition must treat a dd that printed no record counts as "did
// not run" rather than as a failed write, and must not go on to read back.
{
  const body = liftFunction("_writeBootPartition").replace(/^[ \t]*\/\/.*$/gm, "");
  check("_writeBootPartition checks dd actually ran",
        /records in/i.test(body), "no guard on dd's record counts");
  check("_writeBootPartition composes the conv flag rather than hardcoding it",
        body.includes("${T.ddConv}") && !/conv=fsync/.test(body), body.slice(0, 0));
}

// sync is the durability barrier, so a recovery without it is refused rather
// than written to with no barrier.
{
  let threw = null;
  try {
    await deviceTools(fakeClient(
      "TOOL md5sum md5sum\nTOOL dd dd\nTOOL base64 base64\nTOOL tee tee"));
  } catch (e) { threw = e; }
  check("a recovery with no sync is refused", threw !== null, "resolved without a barrier");
  if (threw) check("that refusal names sync", /sync/.test(threw.message), threw.message);
}

// The read-back must sync BEFORE dropping caches: drop_caches evicts only
// clean pages, so the other order can leave the just-written pages in cache
// and confirm the cache rather than the partition.
{
  const body = liftFunction("_writeBootPartition").replace(/^[ \t]*\/\/.*$/gm, "");
  const m = body.match(/\$\{T\.sync\};\s*echo 3 > \/proc\/sys\/vm\/drop_caches/);
  check("read-back syncs before dropping caches", m !== null,
        "expected `${T.sync}; echo 3 > /proc/sys/vm/drop_caches`");
  check("the write is barriered by the probed sync",
        /2>&1; \$\{T\.sync\}/.test(body), "write does not end in the resolved sync");
}

// The resolved set has to include every tool the write path composes into a
// command, or a missing one reaches the device as an empty string.
for (const n of ["md5sum", "dd", "base64", "tee", "sync"]) {
  check(`_TOOL_NAMES includes ${n}`, _TOOL_NAMES.includes(n), _TOOL_NAMES.join(","));
}

if (failures) {
  console.error(`\n${failures} check(s) failed.`);
  process.exit(1);
}
console.log("device_tools: all checks passed.");
