// Screenshots of the web dashboard for the README, from a running serve.
//
//   python3 engine/tools/demo_data.py /tmp/demo-home
//   PATH=/var/empty NETINV_HOME=/tmp/demo-home NETINV_CONFIG=/tmp/demo-home/none \
//     NETINV_CREDENTIALS=/tmp/demo-home/credentials.json \
//     "$(command -v python3)" engine/netinv.py serve --host 127.0.0.1 --port 8799 &
//   npx -y -p playwright@1.61.1 node engine/tools/screenshots.mjs
//
// PATH=/var/empty hides macOS's `security` tool, so serve reads its passcode
// and agent token from the demo's empty credentials.json, not the Keychain;
// NETINV_CONFIG keeps ~/.netinv/config out of it.
//
// Env: NETINV_URL (default http://127.0.0.1:8799), OUT (default
// assets/screenshots next to engine/), CHROMIUM (a browser binary; by default
// Playwright's own, else the newest one in Playwright's cache).
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import { existsSync, readdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const BASE = process.env.NETINV_URL || "http://127.0.0.1:8799";
const OUT = resolve(process.env.OUT || join(here, "..", "..", "assets", "screenshots"));

async function loadPlaywright() {
  try { return await import("playwright"); } catch {}
  // under `npx -p playwright`, the package sits beside a node_modules/.bin on PATH
  for (const dir of (process.env.PATH || "").split(":")) {
    if (dir.endsWith(join("node_modules", ".bin"))) {
      try { return createRequire(join(dir, "..", "x.js"))("playwright"); } catch {}
    }
  }
  throw new Error("playwright not found: run under `npx -y -p playwright@1.61.1 node ...`");
}

function cachedChromium() {
  const cache = join(homedir(), "Library", "Caches", "ms-playwright");
  if (!existsSync(cache)) return undefined;
  const revs = readdirSync(cache).filter((d) => /^chromium-\d+$/.test(d))
    .sort((a, b) => +b.split("-")[1] - +a.split("-")[1]);
  for (const r of revs) {
    for (const app of ["chrome-mac-arm64", "chrome-mac", "chrome-mac-x64"]) {
      for (const name of ["Google Chrome for Testing", "Chromium"]) {
        const p = join(cache, r, app, `${name}.app`, "Contents", "MacOS", name);
        if (existsSync(p)) return p;
      }
    }
  }
  return undefined;
}

// 256-colour palette PNG: a third of the size, no visible change for flat UI.
// Uses pngquant, else ImageMagick, else leaves the file as captured.
function shrink(file) {
  const tries = [["pngquant", ["--force", "--skip-if-larger", "--output", file, "256", file]],
                 ["magick", [file, "-dither", "None", "-colors", "256", `PNG8:${file}`]]];
  for (const [cmd, args] of tries) {
    if (spawnSync(cmd, args, { stdio: "ignore" }).status === 0) break;
  }
  console.log(`  wrote ${file} (${Math.round(statSync(file).size / 1024)} KB)`);
}

const { chromium } = await loadPlaywright();
let browser;
try {
  browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
} catch {
  browser = await chromium.launch({ executablePath: cachedChromium() });
}
const ctx = await browser.newContext({
  viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2, colorScheme: "light",
});
const page = await ctx.newPage();

async function open(path) {
  await page.goto(BASE + path, { waitUntil: "networkidle" });
  await page.waitForFunction(() => !document.body.innerText.includes("loading…"));
  await page.waitForTimeout(400);
}

// Down to the last thing on the page (not the empty viewport below it), at
// most maxH CSS pixels, so long pages stay a sensible size.
async function shoot(file, maxH = 1500) {
  const h = await page.evaluate(() => Math.ceil(24 + Math.max(...[...document.body.children]
    .filter((e) => e.offsetParent !== null && e.tagName !== "DIALOG")
    .map((e) => e.getBoundingClientRect().bottom + scrollY))));
  await page.screenshot({ path: join(OUT, file), fullPage: true,
                          clip: { x: 0, y: 0, width: 1400, height: Math.min(h, maxH) } });
  shrink(join(OUT, file));
}

await open("/");
await shoot("web-devices.png", 1300);

await open("/rooms");
await shoot("web-rooms.png", 1300);

await open("/batteries");
await shoot("web-batteries.png", 1300);

await page.click('tr[data-dev="Garage Camera"]');
await page.waitForSelector("dialog#dv[open]");
await page.waitForTimeout(400);
const box = await page.locator("dialog#dv").boundingBox();
const pad = 12;
await page.screenshot({
  path: join(OUT, "web-battery-history.png"),
  clip: { x: Math.max(0, box.x - pad), y: Math.max(0, box.y - pad),
          width: Math.min(1400, box.width + 2 * pad), height: box.height + 2 * pad },
});
shrink(join(OUT, "web-battery-history.png"));

await browser.close();
