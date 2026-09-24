// List every class name referenced in TSX and report which ones have no rule
// in the stylesheets, so a rewrite can be checked for full coverage.
import { readFileSync, readdirSync } from "node:fs";

const SHEETS = ["src/v7-base.css", "src/v7-app.css"];
const css = SHEETS.map((f) => readFileSync(f, "utf8")).join("\n");
const defined = new Set();
for (const m of css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)) {
  defined.add(m[1]);
}

const files = readdirSync("src").filter((f) => f.endsWith(".tsx"));
const byFile = {};
const all = new Set();
for (const f of files) {
  const src = readFileSync("src/" + f, "utf8");
  const set = new Set();
  for (const m of src.matchAll(/className=(?:"([^"]*)"|'([^']*)'|\{`([^`]*)`\}|\{["'`]([^"'`]*)["'`]\})/g)) {
    const payload = m[1] ?? m[2] ?? m[3] ?? m[4] ?? "";
    for (const t of payload.split(/\s+/)) {
      const c = t.replace(/\$\{.*$/s, "").trim();
      if (c && /^[a-zA-Z][\w-]*$/.test(c)) set.add(c);
    }
  }
  byFile[f] = [...set].sort();
  set.forEach((c) => all.add(c));
}

console.log(`CSS classes defined: ${defined.size}`);
console.log(`classes used in TSX: ${all.size}`);

const missing = [...all].filter((c) => !defined.has(c) && !/[-]$/.test(c)).sort();
console.log(`\n=== used in TSX but no CSS rule (${missing.length}) ===`);
console.log(missing.join(" ") || "(none)");

const unused = [...defined].filter((c) => !all.has(c)).sort();
console.log(`\n=== defined but never referenced in TSX (${unused.length}) ===`);
console.log(unused.join(" ") || "(none)");

for (const [f, list] of Object.entries(byFile)) {
  const gaps = list.filter((c) => !defined.has(c) && !/[-]$/.test(c));
  if (gaps.length) console.log(`\n${f}: ${gaps.join(" ")}`);
}
