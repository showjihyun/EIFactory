import { defineConfig, devices } from "@playwright/test";
import { tmpdir } from "node:os";
import { join } from "node:path";

// 병렬로 도는 다른 작업(worktree)과 겹치지 않게 실행마다 포트와 DB 파일을 고른다.
// 설정 파일은 워커 프로세스에서도 다시 읽히므로, 처음 고른 값을 환경변수로 물려준다.
process.env.E2E_API_PORT ??= String(18000 + (process.pid % 1000));
process.env.E2E_WEB_PORT ??= String(19000 + (process.pid % 1000));
process.env.E2E_DB_PATH ??= join(tmpdir(), `bookmarks-e2e-${process.pid}-${Date.now()}.db`);
const apiPort = process.env.E2E_API_PORT;
const webPort = process.env.E2E_WEB_PORT;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.e2e.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  reporter: "list",
  // 없는 화면 요소를 누르면 테스트 상한(30초)까지 기다리지 않고 5초 뒤 실패한다 (Red 판정이 빠르도록)
  // 화면 작업은 승인 요약에 스크린샷을 붙인다 (factory/config.json의 screenshots)
  use: { baseURL: `http://127.0.0.1:${webPort}`, actionTimeout: 5_000, trace: "off", screenshot: "on" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command:
        "uv run --project ../backend --no-sync uvicorn bookmarks.main:app_from_env --factory " +
        `--app-dir ../backend/src --host 127.0.0.1 --port ${apiPort}`,
      url: `http://127.0.0.1:${apiPort}/api/health`,
      env: { BOOKMARKS_DB_PATH: process.env.E2E_DB_PATH },
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `npx vite --host 127.0.0.1 --port ${webPort} --strictPort`,
      url: `http://127.0.0.1:${webPort}`,
      env: { BOOKMARKS_API_URL: `http://127.0.0.1:${apiPort}` },
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
