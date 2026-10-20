import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "./api";
import App from "./App";
import type { Bookmark } from "./types";

vi.mock("./api");

const bookmark: Bookmark = {
  id: 1,
  url: "https://react.dev",
  title: "React",
  tags: ["react"],
  created_at: "2026-10-20T03:00:00+00:00",
};

async function login() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("아이디"), "demo");
  await user.type(screen.getByLabelText("비밀번호"), "demo");
  await user.click(screen.getByRole("button", { name: "로그인" }));
  return user;
}

beforeEach(() => {
  vi.mocked(api.login).mockResolvedValue(undefined);
  vi.mocked(api.listBookmarks).mockResolvedValue([bookmark]);
  vi.mocked(api.listTags).mockResolvedValue(["react"]);
});

describe("App", () => {
  it("asks for login first", () => {
    render(<App />);
    expect(screen.getByRole("button", { name: "로그인" })).toBeInTheDocument();
    expect(api.listBookmarks).not.toHaveBeenCalled();
  });

  it("shows bookmarks after login", async () => {
    render(<App />);
    await login();
    expect(await screen.findByRole("link", { name: "React" })).toBeInTheDocument();
  });

  it("shows an error with retry when loading fails", async () => {
    vi.mocked(api.listBookmarks).mockRejectedValueOnce(new Error("down"));
    render(<App />);
    const user = await login();
    expect(await screen.findByText("북마크를 불러오지 못했습니다")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    expect(await screen.findByRole("link", { name: "React" })).toBeInTheDocument();
  });

  it("searches with the typed query", async () => {
    render(<App />);
    const user = await login();
    await screen.findByRole("link", { name: "React" });
    await user.type(screen.getByLabelText("검색"), "rea");
    expect(api.listBookmarks).toHaveBeenLastCalledWith({ q: "rea", tag: "" });
  });

  it("filters by tag", async () => {
    render(<App />);
    const user = await login();
    await screen.findByRole("option", { name: "react" });
    await user.selectOptions(screen.getByLabelText("태그 필터"), "react");
    expect(api.listBookmarks).toHaveBeenLastCalledWith({ q: "", tag: "react" });
  });

  it("adds a bookmark and reloads the list", async () => {
    vi.mocked(api.createBookmark).mockResolvedValue(bookmark);
    render(<App />);
    const user = await login();
    await screen.findByRole("link", { name: "React" });
    await user.type(screen.getByLabelText("URL"), "https://vitest.dev");
    await user.type(screen.getByLabelText("제목"), "Vitest");
    await user.click(screen.getByRole("button", { name: "추가" }));
    expect(api.createBookmark).toHaveBeenCalledWith({
      url: "https://vitest.dev",
      title: "Vitest",
      tags: [],
    });
    expect(api.listBookmarks).toHaveBeenCalledTimes(2);
  });

  it("logs out", async () => {
    vi.mocked(api.logout).mockResolvedValue(undefined);
    render(<App />);
    const user = await login();
    await screen.findByRole("link", { name: "React" });
    await user.click(screen.getByRole("button", { name: "로그아웃" }));
    expect(api.logout).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "로그인" })).toBeInTheDocument();
  });
});
