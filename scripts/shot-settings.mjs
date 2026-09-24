// Capture the model-settings modal and the login screen, which need clicking
// or a signed-out state rather than a plain page load.
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const OUT = "output/visual";
mkdirSync(OUT, { recursive: true });
const BASE = process.env.SHOT_URL || "http://127.0.0.1:5201/";

const browser = await chromium.launch({ channel: "chrome" });

// 1. Settings modal, opened from the header's model-config button.
{
  const page = await browser.newPage({ viewport: { width: 1440, height: 1080 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 45000 });
  await page.waitForTimeout(2000);
  await page.click('button[aria-label="模型配置"]').catch(() => {});
  await page.waitForTimeout(2500);
  await page.screenshot({ path: `${OUT}/desktop-settings.png` });
  // switch to the second tab if present, to see the provider editor
  const tabs = page.locator(".settings-tabs button");
  const n = await tabs.count();
  if (n > 1) {
    await tabs.nth(1).click().catch(() => {});
    await page.waitForTimeout(1200);
    await page.screenshot({ path: `${OUT}/desktop-settings-tab2.png` });
  }
  console.log("settings", "tabs=" + n, errors.length ? errors : "ok");
  await page.close();
}

// 2. Narrow settings, to confirm the modal becomes a bottom sheet.
{
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 45000 });
  await page.waitForTimeout(2000);
  await page.click('button[aria-label="模型配置"]').catch(() => {});
  await page.waitForTimeout(2200);
  await page.screenshot({ path: `${OUT}/narrow-settings.png` });
  console.log("narrow settings ok");
  await page.close();
}

await browser.close();
