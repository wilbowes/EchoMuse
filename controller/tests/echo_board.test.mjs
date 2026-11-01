// Tests for _echoBoard() in dashboard.jsx — which Echo the wizard is talking
// to, from the identity /system reports.
//
//     node controller/tests/echo_board.test.mjs
//
// Source extraction rather than import, for the reason pm_verdict.test.mjs
// gives. The board decides whether the wizard runs at all (an emOS image is
// built for the board's own kernel and /system) and whether radar's kernel is
// patched, so both directions matter: every Dot that passed the old check
// must still pass as biscuit, and nothing else may pass as either.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");
const start = src.indexOf("const _echoBoard = (");
if (start < 0) throw new Error("dashboard.jsx no longer defines _echoBoard()");
const body = src.slice(start, src.indexOf("\n};", start) + 3);
const { _echoBoard } = await import("data:text/javascript;base64,"
  + Buffer.from(body + "\nexport { _echoBoard };").toString("base64"));

let fails = 0;
const check = (what, got, want) => {
  if (got === want) console.log(`ok    ${what}`);
  else { console.log(`FAIL  ${what}: got "${got}", wanted "${want}"`); fails++; }
};

// Read off 035T (stock FireOS 6) on 2026-10-23.
check("radar, from /system", _echoBoard("AEORD", "radar_puffin"), "radar");
check("radar by model alone", _echoBoard("AEORD", ""), "radar");
check("radar by codename alone", _echoBoard("", "radar_puffin"), "radar");
check("radar with stray whitespace", _echoBoard(" AEORD\n", "radar_puffin\r"), "radar");

// What the old check accepted stays biscuit.
check("biscuit, from /system", _echoBoard("AEOBC", "csm_biscuit"), "biscuit");
check("TWRP's own biscuit strings", _echoBoard("Echo Dot 2nd Gen", "omni_biscuit"), "biscuit");
check("a model naming Amazon, no codename read", _echoBoard("Amazon Echo Dot", ""), "biscuit");

// Nothing else.
check("Dot 3 (donut)", _echoBoard("", "donut_puffin"), "");
// Read off 41C8 (stock FireOS 6) on 2026-11-01; ro.product.brand is Amazon.
check("Dot 3, from /system", _echoBoard("AEODN", "donut_puffin"), "");
check("Dot 3 with a model naming Amazon", _echoBoard("Amazon Echo Dot", "donut_puffin"), "");
check("Dot 3 under a TWRP build", _echoBoard("Echo Dot 3rd Gen", "omni_donut"), "");
check("Spot (rook)", _echoBoard("AEORK", "rook_puffin"), "");
check("an unread identity", _echoBoard("", ""), "");
check("a codename merely containing radar", _echoBoard("", "radar_puffin_x"), "");

// The detect step refuses '' in BOTH flows. The FireOS flow used to warn
// and carry on, and a Dot 3 in TWRP (release unread) reached biscuit's patch.
const refusal = src.slice(src.indexOf("    if (!board) {"), src.indexOf("\n    }\n", src.indexOf("    if (!board) {")));
check("an unknown board is refused", /throw new Error\(/.test(refusal), true);
check("in every flow", /isEmos|addLog\(/.test(refusal), false);

console.log(fails ? `\n${fails} failure(s)` : "echo_board: all ok");
if (fails) process.exit(1);
