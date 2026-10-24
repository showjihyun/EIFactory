// 대시보드 화면 시험 (verify.py의 scripts/ui 단계). 프론트엔드에 설치된 Playwright를 쓴다.
// 가짜 작업 폴더로 monitor.py를 띄워 화면을 열고, 주요 동작과 보안 성질을 확인한다.
"use strict";
const path = require("path");
const fs = require("fs");
const os = require("os");
const { spawn, execFileSync } = require("child_process");

const root = path.resolve(__dirname, "..", "..");
const { chromium } = require(path.join(root, "frontend", "node_modules", "@playwright", "test"));
const python = process.env.FACTORY_PYTHON || "python";

function fail(message) {
  console.error(`monitor ui: FAIL ${message}`);
  process.exitCode = 1;
}

async function startServer(jobs) {
  const proc = spawn(python, [path.join(root, "scripts", "monitor.py"), "--port", "0", "--no-open", "--jobs-dir", jobs],
    { cwd: root, env: { ...process.env, PYTHONIOENCODING: "utf-8" } });
  const url = await new Promise((resolve, reject) => {
    let out = "";
    const timer = setTimeout(() => reject(new Error(`서버가 뜨지 않음: ${out}`)), 20000);
    proc.stdout.on("data", (d) => {
      out += d.toString();
      const m = out.match(/http:\/\/127\.0\.0\.1:\d+\//);
      if (m) { clearTimeout(timer); resolve(m[0]); }
    });
    proc.stderr.on("data", (d) => { out += d.toString(); });
    proc.on("exit", (code) => reject(new Error(`서버 종료 ${code}: ${out}`)));
  }).catch((e) => { proc.kill(); throw e; });
  return { proc, url };
}

(async () => {
  const jobs = fs.mkdtempSync(path.join(os.tmpdir(), "monitor-ui-"));
  execFileSync(python, [path.join(__dirname, "fixture.py"), jobs]);
  let proc = null;
  let browser = null;
  try {
    const server = await startServer(jobs);
    proc = server.proc;
    const url = server.url;
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    let dialogs = 0;
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    page.on("pageerror", (e) => errors.push(String(e)));
    page.on("dialog", async (d) => { dialogs += 1; await d.dismiss(); });
    await page.goto(url);
    await page.waitForSelector(".card[data-key='J002']", { timeout: 10000 });

    const cards = await page.locator(".card").count();
    if (cards < 4) fail(`카드 ${cards}개 (4개 이상이어야 함)`);
    const xss = await page.locator(".card[data-key='J004'] .req").textContent();
    if (!xss.includes("<img src=x onerror")) fail("XSS 문자열이 글자로 보이지 않음");
    if (await page.locator("img[src='x']").count()) fail("세션이 쓴 문자열이 HTML로 들어감");
    const attention = await page.locator("#attention .acard").count();
    if (attention < 2) fail(`주의 줄 ${attention}건 (승인 대기·막힘·오류)`);
    if (!(await page.locator("#attention").textContent()).includes("dispatch.py --approve J002")) fail("승인 명령이 없음");
    if ((await page.locator("#top").textContent()).includes("null")) fail("윗줄에 'null' 글자");
    await page.waitForFunction(() => document.querySelector("#lanes") && document.querySelector("#lanes").textContent.includes("먼저 머지됨"), null, { timeout: 10000 }).catch(() => {});
    const lanesText = await page.locator("#lanes").textContent();
    if (!lanesText.includes("J001 (먼저 머지됨) → J005") || !lanesText.includes("List.tsx")) fail("머지 후 ⚡ 예고(J001 → J005)가 없음");
    const unpriced = await page.locator(".card[data-key='J001'] .meter .num").last().textContent();
    if (!unpriced.includes("≈ ?")) fail(`단가 없는 작업이 '≈ ?'가 아님 (${unpriced})`);

    await page.click(".card[data-key='J002']");
    await page.waitForSelector("#drawer.open .sec", { timeout: 10000 });
    await page.waitForFunction(() => document.querySelector("#drawer").textContent.includes("검증 증거"), null, { timeout: 10000 });
    const drawer = await page.locator("#drawer").textContent();
    for (const want of ["다음 명령", "검증 증거", "리뷰 (1회차)", "모델·인증", "비용·토큰", "dispatch.py --approve J002"]) {
      if (!drawer.includes(want)) fail(`드로어에 '${want}' 없음`);
    }
    await page.locator("#drawer details summary", { hasText: "원문 보기 (code)" }).click();
    await page.waitForFunction(() => [...document.querySelectorAll("#drawer pre.raw")].some((p) => p.textContent.includes("<script>")), null, { timeout: 10000 });
    await page.keyboard.press("Escape");

    await page.click("button[aria-label='설정 보기']");
    await page.waitForFunction(() => document.querySelector("#drawer").textContent.includes("factory_settings.py"), null, { timeout: 10000 });
    await page.keyboard.press("Escape");

    await page.emulateMedia({ reducedMotion: "reduce" });
    const anim = await page.evaluate(() => { const n = document.querySelector(".bounce, .live-dot, .seg.live"); return n ? getComputedStyle(n).animationName : "none"; });
    if (anim !== "none") fail(`동작 줄이기에서 애니메이션이 남음 (${anim})`);

    if (dialogs) fail(`대화상자 ${dialogs}개 (스크립트가 실행됨)`);
    const real = errors.filter((e) => !e.includes("favicon"));
    if (real.length) fail(`콘솔 오류: ${real.join(" | ")}`);
  } finally {
    if (browser) await browser.close();
    if (proc) proc.kill();
    fs.rmSync(jobs, { recursive: true, force: true });
  }
  if (!process.exitCode) console.log("monitor ui: PASS");
})().catch((e) => { fail(String(e && e.stack || e)); });
