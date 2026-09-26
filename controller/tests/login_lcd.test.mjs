// The login page's status line renders text, never markup.
//
//     node controller/tests/login_lcd.test.mjs
//
// setLcd in static/index.html shows server error strings. It built its span
// with innerHTML, so any error text containing markup would have run as HTML
// on the page that takes the admin password. Drives the real function,
// lifted from the page, against a minimal DOM.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

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

let innerHTMLWrites = 0;
const lcd = {
  children: [],
  replaceChildren(...c) { this.children = c; },
  set innerHTML(_) { innerHTMLWrites++; },
};
const document = {
  createElement: (tag) => ({
    tag, style: {}, textContent: "",
    set innerHTML(_) { innerHTMLWrites++; },
  }),
};
const setLcd = new Function("lcd", "document",
  `${liftFunction("setLcd")}\nreturn setLcd;`)(lcd, document);

let failures = 0;
function check(name, cond, detail) {
  if (cond) return;
  failures++;
  console.error(`FAIL: ${name}${detail ? `\n      ${detail}` : ""}`);
}

const hostile = '<img src=x onerror="alert(1)">';
setLcd(hostile, "#c04040");
const span = lcd.children[0];

check("one child is rendered", lcd.children.length === 1);
check("markup arrives as text", span && span.textContent === hostile,
      JSON.stringify(span));
check("nothing is written through innerHTML", innerHTMLWrites === 0);
check("colour is kept", span && span.style.color === "#c04040");
check("glow is kept", span && span.style.textShadow === "0 0 10px #c0404044");

setLcd("Signed in", "#9aba80");
check("a second call replaces the first", lcd.children.length === 1
      && lcd.children[0].textContent === "Signed in");

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("login_lcd: all checks passed");
