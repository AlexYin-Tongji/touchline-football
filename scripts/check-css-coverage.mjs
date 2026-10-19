// Report class names used in TSX that have no rule in the stylesheets.
//
// A stylesheet rewrite compiles and runs even when a selector matches nothing,
// so the only way to catch a dropped rule is to diff names. Run after any
// structural change:
//
//   node scripts/check-css-coverage.mjs
//
// Output is a report, not a gate. "Defined but not used" includes selectors
// that are applied by state classes built at runtime and a few names kept for
// element structure; treat it as a list to review, not to delete blindly.
import { readFileSync, readdirSync } from "node:fs";

const STYLE_DIR = "src/styles";
const sheets = readdirSync(STYLE_DIR).filter((f) => f.endsWith(".css"));

/** Selector text of every rule, with comments and declaration blocks removed. */
function selectorText(source) {
  const withoutComments = source.replace(/\/\*[\s\S]*?\*\//g, "");
  const out = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < withoutComments.length; i += 1) {
    const ch = withoutComments[i];
    if (ch === "{") {
      if (depth === 0) {
        const head = withoutComments.slice(start, i).trim();
        // Skip at-rule heads (`@media …`), whose children are visited next.
        if (!head.startsWith("@")) out.push(head);
      }
      depth += 1;
    } else if (ch === "}") {
      depth = Math.max(0, depth - 1);
      start = i + 1;
    }
  }
  return out.join("\n");
}

const css = sheets.map((f) => selectorText(readFileSync(`${STYLE_DIR}/${f}`, "utf8"))).join("\n");

// Custom property declarations (`--t-body: …`) are gone by now; what remains
// is selector text, so any `.name` here is a class.
const defined = new Set();
for (const match of css.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)) defined.add(match[1]);

// Names composed at runtime or used as state modifiers.
const RUNTIME = new Set([
  "active", "selected", "open", "is-open", "chosen", "current", "wide", "small",
  "primary", "compact", "danger", "armed", "full_text", "partial", "summary",
  "dark-button", "load-more", "history-more", "manage-models", "model-id",
  "model-name", "player-photo", "player-season-line", "player-no-photo",
  "read-more", "story-ask", "settings-modal", "stop", "on", "warn", "bad",
  "success", "error", "meta", "large", "following", "goal-for", "goals-for",
  "goals-against", "is-home", "is-away", "is-tentative", "is-retryable",
  "is-active", "large-type", "result-W", "result-D", "result-L", "legend-W",
  "legend-D", "legend-L", "step-completed", "step-success", "step-failed",
  "step-error", "step-running", "step-skipped", "status-running",
  "status-queued", "status-failed", "tone-running", "tone-fresh", "tone-gap",
  "tone-fail", "tone-idle",
]);

// Class literals in JSX: whole strings, template literals, and conditional
// fragment literals such as `result-text-${match.result}`.
const used = new Set();
const files = readdirSync("src").filter((f) => f.endsWith(".tsx"));
for (const file of files) {
  const source = readFileSync(`src/${file}`, "utf8");
  for (const match of source.matchAll(/className=(?:"([^"]*)"|\{`([^`]*)`\}|\{`([^`]*)`\})/g)) {
    for (const literal of [match[1], match[2], match[3]]) {
      if (!literal) continue;
      // Keep the literal prefix of `${…}` interpolations: `skill-${id}`
      // contributes `skill-`, which never matches a full rule and is skipped.
      for (const token of literal.split(/[\s]+/)) {
        const name = token.replace(/\$\{[^}]*\}/g, "").replace(/[{}]/g, "");
        if (/^[-a-zA-Z][\w-]*$/.test(name) && !name.endsWith("-")) used.add(name);
      }
    }
  }
}

const missing = [...used]
  .filter((c) => !defined.has(c) && !RUNTIME.has(c))
  .sort();
const unused = [...defined].filter((c) => !used.has(c) && !RUNTIME.has(c)).sort();

console.log(`stylesheets: ${sheets.join(", ")}`);
console.log(`\nused but not defined (${missing.length}):`);
for (const name of missing) console.log(`  .${name}`);
console.log(`\ndefined but not used (${unused.length}):`);
for (const name of unused) console.log(`  .${name}`);
