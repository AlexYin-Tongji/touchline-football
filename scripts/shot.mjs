// Visual check for UI changes: captures the dev server at desktop and narrow
// widths into output/visual/ (git-ignored). AGENTS.md requires UI work to be
// verified at both widths.
//
// Uses the system Chrome via `channel`, so no Playwright browser download is
// needed. Playwright itself is not a project dependency; install it on demand:
//   npm install --no-save playwright && node scripts/shot.mjs
//
// Requires a dev server: npx vite --host 127.0.0.1 --port 5199
// Point it elsewhere with SHOT_URL.
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const BASE = process.env.SHOT_URL || "http://127.0.0.1:5199/";
const OUT = "output/visual";
mkdirSync(OUT, { recursive: true });

const sizes = [
  { name: "desktop", width: 1440, height: 950 },
  { name: "narrow", width: 390, height: 844 },
];

const browser = await chromium.launch({ channel: "chrome" });
for (const size of sizes) {
  const page = await browser.newPage({
    viewport: { width: size.width, height: size.height },
    deviceScaleFactor: 1,
  });
  const problems = [];
  page.on("console", (m) => m.type() === "error" && problems.push(m.text()));
  page.on("pageerror", (e) => problems.push(String(e)));
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 45000 });
  await page.waitForTimeout(2500);
  for (const [label, selector] of [
    ["feed", 'button:has-text("动态")'],
    ["team", 'button:has-text("球队")'],
    ["assistant", 'button:has-text("问助手")'],
  ]) {
    const tab = page.locator(selector).first();
    if (await tab.count()) {
      await tab.click().catch(() => {});
      await page.waitForTimeout(2200);
      await page.screenshot({ path: `${OUT}/${size.name}-${label}.png`, fullPage: false });
    }
  }
  console.log(
    JSON.stringify({ size: size.name, errors: problems.slice(0, 8) }, null, 1),
  );
  await page.close();
}
await browser.close();
