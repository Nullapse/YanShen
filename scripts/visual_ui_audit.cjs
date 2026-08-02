const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const output = path.join(root, "design", "visual-audit-1.3.4");
const port = process.env.VISUAL_QA_PORT || "5169";
fs.mkdirSync(output, { recursive: true });

const routes = [
  ["home", "/home"],
  ["papers", "/papers"],
  ["questions", "/"],
  ["favorites", "/favorites"],
  ["question-detail", "/questions/563"],
  ["paper-detail", "/papers/285?q=563"],
  ["attempts", "/attempts"],
  ["attempts-ungraded", "/attempts?status=ungraded"],
  ["attempt-detail", "/attempts/8"],
  ["attempt-report", "/attempts/7"],
  ["grading-report", "/grading-reports/8"],
  ["notes", "/notes"],
  ["statistics", "/statistics"],
  ["ai-coach", "/agent"],
  ["ai-setup", "/agent/setup"],
  ["ai-memories", "/agent/memories"],
  ["ai-evals", "/agent/evals"],
  ["settings", "/settings"],
  ["import", "/import"],
  ["coverage", "/coverage"]
];
const requestedNames = new Set((process.env.VISUAL_QA_NAMES || "").split(",").filter(Boolean));
const activeRoutes = requestedNames.size ? routes.filter(([name]) => requestedNames.has(name)) : routes;

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
  });
  const desktop = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
  const failures = [];
  const results = [];

  desktop.on("console", (message) => {
    if (message.type() === "error") failures.push(`console: ${message.text()}`);
  });
  desktop.on("pageerror", (error) => failures.push(`pageerror: ${error.message}`));

  for (const [name, route] of activeRoutes) {
    process.stdout.write(`desktop ${name}\n`);
    const response = await desktop.goto(`http://127.0.0.1:${port}${route}`, { waitUntil: "domcontentloaded", timeout: 20000 });
    assert(response, `${route} returned no response`);
    assert.equal(response.status(), 200, `${route} returned ${response.status()}`);
    await desktop.waitForLoadState("networkidle", { timeout: 800 }).catch(() => {});
    await desktop.evaluate(() => window.scrollTo(0, 0));
    await desktop.locator(".sidebar").waitFor({ state: "visible" });
    await desktop.locator(".main").waitFor({ state: "visible" });
    const metrics = await desktop.evaluate(() => ({
      bodyWidth: document.body.scrollWidth,
      viewportWidth: document.documentElement.clientWidth,
      title: document.title,
      active: document.body.dataset.activeSection || ""
    }));
    const overflow = metrics.bodyWidth - metrics.viewportWidth;
    assert(overflow <= 2, `${route} has ${overflow}px page-level horizontal overflow`);
    await desktop.screenshot({ path: path.join(output, `${name}.png`), fullPage: name === "home" });
    results.push({ name, route, ...metrics, overflow });
  }

  if (!requestedNames.size || requestedNames.has("home")) {
    const wideShort = await browser.newPage({ viewport: { width: 2048, height: 707 }, deviceScaleFactor: 1 });
    process.stdout.write("wide-short home\n");
    const response = await wideShort.goto(`http://127.0.0.1:${port}/home`, { waitUntil: "domcontentloaded", timeout: 20000 });
    assert.equal(response.status(), 200);
    await wideShort.waitForLoadState("networkidle", { timeout: 800 }).catch(() => {});
    const metrics = await wideShort.evaluate(() => ({
      horizontalOverflow: document.body.scrollWidth - document.documentElement.clientWidth,
      verticalOverflow: document.body.scrollHeight - document.documentElement.clientHeight,
      dashboardBottom: Math.ceil(document.querySelector(".home-dashboard").getBoundingClientRect().bottom),
      viewportHeight: document.documentElement.clientHeight,
      clock: document.querySelector("[data-home-clock]")?.textContent || ""
    }));
    assert(metrics.horizontalOverflow <= 2, `home wide-short has ${metrics.horizontalOverflow}px horizontal overflow`);
    assert(metrics.verticalOverflow <= 2, `home wide-short has ${metrics.verticalOverflow}px vertical overflow`);
    assert(metrics.dashboardBottom <= metrics.viewportHeight, `home dashboard ends at ${metrics.dashboardBottom}px`);
    assert(metrics.clock.includes(":"), "home clock did not render current time");
    assert.equal(await wideShort.locator("[data-home-plan-form]").getAttribute("data-home-plan-ready"), "true");
    await wideShort.locator("[data-home-plan-open]").click();
    await wideShort.locator("[data-home-plan-modal]").waitFor({ state: "visible" });
    await wideShort.screenshot({ path: path.join(output, "home-plan-modal.png"), fullPage: false });
    await wideShort.locator("[data-home-plan-title]").first().fill("重点复盘");
    await wideShort.locator("[data-home-plan-minutes]").first().fill("12");
    await wideShort.locator("[data-home-plan-form] button[type='submit']").click();
    await wideShort.locator("[data-home-plan-item] strong").first().waitFor({ state: "visible" });
    assert.equal(await wideShort.locator("[data-home-plan-item] strong").first().textContent(), "重点复盘");
    await wideShort.reload({ waitUntil: "domcontentloaded" });
    assert.equal(await wideShort.locator("[data-home-plan-item] strong").first().textContent(), "重点复盘");
    await wideShort.evaluate(() => window.localStorage.clear());
    await wideShort.screenshot({ path: path.join(output, "home-wide-short.png"), fullPage: false });
    results.push({ name: "home-wide-short", route: "/home", ...metrics });
    await wideShort.close();
  }

  const compact = await browser.newPage({ viewport: { width: 1024, height: 900 }, deviceScaleFactor: 1 });
  for (const [name, route] of activeRoutes.filter(([name]) => ["home", "papers", "question-detail", "attempt-detail", "attempt-report", "statistics", "ai-coach", "settings"].includes(name))) {
    process.stdout.write(`compact ${name}\n`);
    const response = await compact.goto(`http://127.0.0.1:${port}${route}`, { waitUntil: "domcontentloaded", timeout: 20000 });
    assert.equal(response.status(), 200);
    await compact.waitForLoadState("networkidle", { timeout: 800 }).catch(() => {});
    await compact.evaluate(() => window.scrollTo(0, 0));
    const overflow = await compact.evaluate(() => document.body.scrollWidth - document.documentElement.clientWidth);
    assert(overflow <= 2, `${route} compact view has ${overflow}px page-level horizontal overflow`);
    await compact.screenshot({ path: path.join(output, `${name}-compact.png`), fullPage: name === "home" });
  }

  await browser.close();
  assert.deepEqual(failures, []);
  fs.writeFileSync(path.join(output, "audit.json"), JSON.stringify(results, null, 2));
  process.stdout.write(`Visual audit passed for ${activeRoutes.length} interfaces.\n`);
})().catch((error) => {
  process.stderr.write(`${error.stack}\n`);
  process.exit(1);
});
