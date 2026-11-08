# DESIGN.md (템플릿)

> 팩토리 키트가 복사한 템플릿이다 (factory-v10). 대상 레포 루트에 `DESIGN.md`로 복사해 제품에 맞게 채우고, **사람이 승인해 커밋**한다. 승인된 `DESIGN.md`는 잠금·HIGH 파일이라 팩토리 작업(Builder)은 따르기만 하고 고치지 못한다. 화면을 바꾸는 계획은 `DESIGN.md`가 없으면 `plan.py check`에서 거부된다.

## 0. 디자인 원본

- 사용자가 준 디자인 URL(Figma 등)이 있으면 **그 URL이 기준**이다. 여기에 URL과 화면별 프레임 링크를 적고, 아래 토큰을 그 디자인에서 옮긴다.
- 디자인 URL이 없으면 **shadcn/ui**(https://ui.shadcn.com)를 기준으로 한다. 아래 기본값은 shadcn/ui 기본 테마(neutral)다.
- 원본: `<디자인 URL 또는 "shadcn/ui 기본 테마 (neutral)">`

## 1. 기술 기준

| 항목 | 기준 |
|---|---|
| 프레임워크 | TypeScript + React |
| 스타일 | Tailwind CSS v4 (`@import "tailwindcss"`, `@theme inline`), 색은 CSS 변수(oklch) |
| 컴포넌트 | shadcn/ui (`npx shadcn@latest add <이름>`), 위치 `src/components/ui/`, 경로 별칭 `@/*` → `src/*` |
| 아이콘 | lucide-react |
| 클래스 합치기 | `cn()` (`src/lib/utils.ts`, clsx + tailwind-merge) |

## 2. 토큰

색은 의미 쌍으로 쓴다: 바탕 토큰(`primary`)과 그 위 글자 토큰(`primary-foreground`). 화면 코드에는 토큰 이름(`bg-primary`, `text-muted-foreground`, `border-border`)만 쓰고 `#hex`·`rgb()`·인라인 style을 쓰지 않는다.

| 토큰 | 라이트 | 다크 | 쓰는 곳 |
|---|---|---|---|
| `--background` / `--foreground` | `oklch(1 0 0)` / `oklch(0.145 0 0)` | `oklch(0.145 0 0)` / `oklch(0.985 0 0)` | 페이지 바탕, 본문 |
| `--card` / `--card-foreground` | `oklch(1 0 0)` / `oklch(0.145 0 0)` | `oklch(0.205 0 0)` / `oklch(0.985 0 0)` | 카드·패널 |
| `--primary` / `--primary-foreground` | `oklch(0.205 0 0)` / `oklch(0.985 0 0)` | `oklch(0.922 0 0)` / `oklch(0.205 0 0)` | 주 버튼, 선택 상태 |
| `--secondary` / `--secondary-foreground` | `oklch(0.97 0 0)` / `oklch(0.205 0 0)` | `oklch(0.269 0 0)` / `oklch(0.985 0 0)` | 보조 버튼 |
| `--muted` / `--muted-foreground` | `oklch(0.97 0 0)` / `oklch(0.556 0 0)` | `oklch(0.269 0 0)` / `oklch(0.708 0 0)` | 보조 글, 빈 상태, 날짜 |
| `--accent` / `--accent-foreground` | `oklch(0.97 0 0)` / `oklch(0.205 0 0)` | `oklch(0.269 0 0)` / `oklch(0.985 0 0)` | 마우스 올림, 선택 행 |
| `--destructive` | `oklch(0.577 0.245 27.325)` | `oklch(0.704 0.191 22.216)` | 삭제·오류 |
| `--border` / `--input` / `--ring` | `oklch(0.922 0 0)` / `oklch(0.922 0 0)` / `oklch(0.708 0 0)` | `oklch(1 0 0 / 10%)` / `oklch(1 0 0 / 15%)` / `oklch(0.556 0 0)` | 테두리, 입력칸, 포커스 링 |
| `--radius` | `0.625rem` | (같음) | 반경 기준 (`rounded-md` 등은 이 값에서 파생) |

제품 강조색이 필요하면 `--primary`만 바꾸고 이 표에 근거를 적는다.

## 3. 글자·간격·배치

- 글꼴: 시스템 글꼴 스택 (한국어는 `"Pretendard", "Apple SD Gothic Neo", "Malgun Gothic", system-ui, sans-serif`).
- 크기: 페이지 제목 `text-2xl font-semibold`, 구역 제목 `text-lg font-semibold`, 본문 `text-sm`, 보조 `text-xs text-muted-foreground`.
- 간격: Tailwind 4px 눈금만 (`gap-2`, `p-4`, `space-y-6`). 카드 안쪽 `p-6`, 구역 사이 `gap-6`.
- 배치: `<채울 것: 예) 상단 헤더 + 왼쪽 탐색 + 본문 최대 폭 960px>`. 1280px 이상, 768px, 360px(휴대폰)에서 가로 스크롤이 없어야 한다.

## 4. 컴포넌트 대응

| 화면 요소 | shadcn/ui 컴포넌트 | 규칙 |
|---|---|---|
| 주 동작 버튼 | `Button` (default) | 한 화면에 하나 |
| 보조 동작 | `Button` (`variant="outline"` 또는 `"ghost"`) | |
| 삭제 | `Button` (`variant="destructive"`) + `AlertDialog` 확인 | |
| 입력 | `Input`, `Textarea`, `Label` | 모든 입력에 `Label` |
| 목록 항목·패널 | `Card` | |
| 탭 전환 | `Tabs` | |
| 상태 표시 | `Badge` | |
| 알림·결과 | `sonner` 토스트 또는 `Alert` | |
| 로딩 | `Skeleton`, 버튼 안 `Loader2` 아이콘 + `disabled` | |

## 5. 상태

모든 화면은 다음 상태를 보여 준다: 빈 목록(설명 + 다음 동작 버튼), 로딩(Skeleton), 오류(`Alert variant="destructive"` + 다시 시도), 요청 중(버튼 막기).

## 6. 접근성

- 버튼·입력의 접근 가능한 이름(테스트가 찾는 이름)을 바꾸지 않는다.
- 포커스 링(`ring`)을 지우지 않는다. 대비는 WCAG AA (본문 4.5:1).
- 키보드만으로 모든 동작이 된다.

## 7. 화면 목록

`<채울 것: 화면마다 목적, 배치, 쓰는 컴포넌트, 상태. 디자인 URL이 있으면 프레임 링크>`

## 8. 승인

- 승인: `<사람 이름 또는 역할>`, `<날짜>`. 바꿀 때는 사람이 고치고 다시 승인한다.
