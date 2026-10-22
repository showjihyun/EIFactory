import { expect, test, type Page } from "@playwright/test";

// 사용자 흐름 테스트 (기획안 5장 "UI 흐름 (Phase 3)"). 백엔드와 화면을 실제로 띄워 브라우저로 확인한다.

async function login(page: Page, password = "demo") {
  await page.goto("/");
  await page.getByLabel("아이디").fill("demo");
  await page.getByLabel("비밀번호").fill(password);
  await page.getByRole("button", { name: "로그인" }).click();
}

test("잘못된 비밀번호로는 로그인되지 않는다", async ({ page }) => {
  await login(page, "wrong-password");
  await expect(page.getByText("로그인에 실패했습니다")).toBeVisible();
  await expect(page.getByRole("button", { name: "로그아웃" })).toHaveCount(0);
});

test("북마크를 추가하고 검색한 뒤 삭제한다", async ({ page }) => {
  await login(page);
  await expect(page.getByRole("button", { name: "로그아웃" })).toBeVisible();

  const title = `E2E 북마크 ${Date.now()}`;
  await page.getByLabel("URL").fill("https://example.com/e2e");
  await page.getByLabel("제목").fill(title);
  await page.getByLabel("태그 (쉼표로 구분)").fill("e2e, 테스트");
  await page.getByRole("button", { name: "추가" }).click();

  const item = page.getByTestId("bookmark-item").filter({ hasText: title });
  await expect(item).toBeVisible();

  await page.getByLabel("검색").fill(title);
  await expect(page.getByTestId("bookmark-item")).toHaveCount(1);

  page.once("dialog", (dialog) => void dialog.accept());
  await item.getByRole("button", { name: "삭제" }).click();
  await expect(item).toHaveCount(0);
});
