// Cross-browser test of goldcheck's HTML reports, plus README screenshots and a short video.
// Usage (from this folder):  npm i -D playwright@1.61.1 && npx playwright install chromium webkit
//                            node report-test.js
// Same 11 setups as the 3 a.m. Incident kit: Windows, Mac, iPhone, Android, iPad, 320 px.
const { chromium, webkit, devices } = require('playwright');
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const OUT = path.join(__dirname, 'shots');
const MEDIA = path.join(ROOT, 'docs', 'media');
fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });
fs.mkdirSync(MEDIA, { recursive: true });

const PY = process.env.PYTHON || 'python';
const DEMO = path.join(OUT, 'demo');
execFileSync(PY, ['-m', 'goldcheck', 'demo', '--out', DEMO], { cwd: ROOT, stdio: 'ignore' });
try {
  execFileSync(PY, ['-m', 'goldcheck', 'compare', '--db', path.join(DEMO, 'demo.sqlite'),
    '--old', path.join(DEMO, 'old_query.sql'), '--new', path.join(DEMO, 'new_query.sql'),
    '--out', path.join(DEMO, 'compare.html')], { cwd: ROOT, stdio: 'ignore' });
} catch (e) { /* exit code 1 = the pair differs, as intended */ }
const url = (f) => 'file:///' + path.join(DEMO, f).replace(/\\/g, '/');

const D = (name) => { const d = { ...devices[name] }; delete d.defaultBrowserType; return d; };
const PROFILES = [
  ['Windows', 'Chrome 1366', chromium, { viewport: { width: 1366, height: 768 } }],
  ['Windows', 'Edge 1920', chromium, { viewport: { width: 1920, height: 1080 } }, { channel: 'msedge' }],
  ['Mac', 'Safari (WebKit) 1440', webkit, { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 }],
  ['Mac', 'Chrome 1280', chromium, { viewport: { width: 1280, height: 800 } }],
  ['iPhone', 'iPhone SE (375)', webkit, D('iPhone SE')],
  ['iPhone', 'iPhone 13 mini', webkit, D('iPhone 13 Mini')],
  ['iPhone', 'iPhone 15 Pro Max', webkit, D('iPhone 15 Pro Max')],
  ['iPhone', 'Small phone 320 (WebKit)', webkit, { ...D('iPhone SE'), viewport: { width: 320, height: 568 } }],
  ['Android', 'Pixel 7 (Chrome)', chromium, D('Pixel 7')],
  ['Android', 'Galaxy S9+ (Chrome, 320)', chromium, D('Galaxy S9+')],
  ['iPad', 'iPad Mini landscape', webkit, D('iPad Mini landscape')],
];

async function checks(page, file, expectCx) {
  const fails = [];
  await page.goto(url(file));
  const r = await page.evaluate(() => {
    const w = window.innerWidth;
    const overflow = document.documentElement.scrollWidth > w + 1;
    const wide = [...document.querySelectorAll('main *')].filter((e) => {
      if (e.closest('.wrap, pre')) return false;
      const b = e.getBoundingClientRect();
      return b.width > 0 && b.right > w + 1;
    }).slice(0, 3).map((e) => e.tagName + '.' + e.className);
    const small = [...document.querySelectorAll('main p, main td, main summary span, main pre')]
      .filter((e) => parseFloat(getComputedStyle(e).fontSize) < 12).length;
    return { overflow, wide, small, cx: document.querySelectorAll('.cx').length,
             blame: document.querySelectorAll('.blame').length, bg: getComputedStyle(document.body).backgroundColor };
  });
  if (r.overflow) fails.push(`${file}: page scrolls sideways`);
  if (r.wide.length) fails.push(`${file}: elements past the right edge: ${r.wide.join(', ')}`);
  if (r.small) fails.push(`${file}: ${r.small} text elements under 12 px`);
  if (r.cx !== expectCx) fails.push(`${file}: ${r.cx} counterexample sections, expected ${expectCx}`);
  if (r.blame !== expectCx) fails.push(`${file}: ${r.blame} blame sections, expected ${expectCx}`);
  // A closed question opens on tap/click.
  const closed = await page.$('details:not([open]) > summary');
  if (closed) {
    await closed.scrollIntoViewIfNeeded();
    await closed.click();
    if (!(await closed.evaluate((s) => s.parentElement.open))) fails.push(`${file}: tapping a question does not open it`);
  }
  return { fails, bg: r.bg };
}

(async () => {
  const results = [];
  for (const [platform, label, engine, ctxOpts, launchOpts] of PROFILES) {
    const fails = [];
    let browser;
    try {
      browser = await engine.launch(launchOpts || {});
    } catch (e) {
      results.push([platform, label, 'NOT RUN', [String(e.message).split('\n')[0]]]);
      continue;
    }
    const bgs = {};
    for (const scheme of ['light', 'dark']) {
      const ctx = await browser.newContext({ ...ctxOpts, colorScheme: scheme });
      const page = await ctx.newPage();
      for (const [file, n] of [['report.html', 4], ['compare.html', 1]]) {
        const r = await checks(page, file, n);
        fails.push(...r.fails.map((f) => `${scheme}: ${f}`));
        bgs[scheme] = r.bg;
      }
      if (scheme === 'light') {
        await page.goto(url('report.html'));
        await page.screenshot({ path: path.join(OUT, `${platform}-${label}.png`.replace(/[^\w.-]+/g, '_')), fullPage: true });
      }
      await ctx.close();
    }
    if (bgs.light === bgs.dark) fails.push('dark mode has the same background as light mode');
    await browser.close();
    results.push([platform, label, fails.length ? 'FAIL' : 'PASS', fails]);
    console.log(`${fails.length ? 'FAIL' : 'PASS'}  ${platform.padEnd(8)} ${label}${fails.length ? '\n   ' + fails.join('\n   ') : ''}`);
  }

  // README media: desktop report, the counterexample panel, a phone view, and a short video.
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1200, height: 900 }, deviceScaleFactor: 2,
    recordVideo: { dir: OUT, size: { width: 1200, height: 900 } } });
  const page = await ctx.newPage();
  await page.goto(url('report.html'));
  await page.waitForTimeout(1200);
  await page.screenshot({ path: path.join(MEDIA, 'report-top.png') });
  const q08 = page.locator('details', { hasText: 'q08' });
  await q08.scrollIntoViewIfNeeded();
  await page.waitForTimeout(800);
  const cx = q08.locator('.cx');
  await cx.scrollIntoViewIfNeeded();
  await page.mouse.wheel(0, 250);
  await page.waitForTimeout(1500);
  await cx.screenshot({ path: path.join(MEDIA, 'counterexample.png') });
  await page.goto(url('compare.html'));
  await page.waitForTimeout(1500);
  await page.locator('.cx').scrollIntoViewIfNeeded();
  await page.waitForTimeout(2000);
  const video = await page.video().path();
  await ctx.close();
  fs.copyFileSync(video, path.join(MEDIA, 'demo.webm'));
  const phone = await (await webkit.launch()).newContext(D('iPhone 13 Mini'));
  const pp = await phone.newPage();
  await pp.goto(url('report.html'));
  await pp.screenshot({ path: path.join(MEDIA, 'report-phone.png') });
  await phone.browser().close();
  await browser.close();

  const pass = results.filter((r) => r[2] === 'PASS').length;
  console.log(`\n${pass}/${results.length} setups passed. Screenshots: ${OUT}. README media: ${MEDIA}`);
  fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify(results, null, 1));
  process.exit(pass === results.length ? 0 : 1);
})();
