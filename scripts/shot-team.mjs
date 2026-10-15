// Screenshots of the three team sub-sections, full page, at both widths.
//
// The generic shot.mjs only captures whatever the first paint shows, which for
// a page this tall is one screen of the hero. Reading a layout decision needs
// the whole document, so each tab is captured full-page after its panel has
// actually rendered — waiting on the content, not on a fixed timeout, because
// the squad and fixture panels fetch separately from the hero.
//
// Requires the dev server: TOUCHLINE_PORT=9988 npx vite --host 127.0.0.1 --port 5199
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const BASE = process.env.SHOT_URL || "http://127.0.0.1:5199/";
const OUT = "output/visual";
mkdirSync(OUT, { recursive: true });

const TABS = [
  { key: "info", label: "球队信息", waitFor: ".identity-grid" },
  { key: "squad", label: "球员信息", waitFor: ".player-grid" },
  { key: "fixtures", label: "未来比赛", waitFor: ".fixture-timeline" },
];

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
  const teamTab = page.locator('button:has-text("球队")').first();
  if (await teamTab.count()) await teamTab.click();
  await page.waitForTimeout(3000);
  for (const tab of TABS) {
    const button = page.locator(`button:has-text("${tab.label}")`).last();
    if (!(await button.count())) {
      problems.push(`missing tab ${tab.label}`);
      continue;
    }
    await button.click();
    try {
      await page.waitForSelector(tab.waitFor, { timeout: 20000 });
    } catch {
      problems.push(`${tab.label}: ${tab.waitFor} never rendered`);
    }
    // Let images settle so the capture shows the real thing, not placeholders.
    await page.waitForTimeout(1800);
    await page.screenshot({ path: `${OUT}/${size.name}-team-${tab.key}.png`, fullPage: true });
  }
  console.log(JSON.stringify({ size: size.name, errors: problems }));
  await page.close();
}
await browser.close();
