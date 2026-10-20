import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Bookmark } from "../types";
import { BookmarkList } from "./BookmarkList";

const bookmark: Bookmark = {
  id: 1,
  url: "https://fastapi.tiangolo.com",
  title: "FastAPI Docs",
  tags: ["python", "docs"],
  created_at: "2026-10-20T03:00:00+00:00",
};

function renderList(bookmarks: Bookmark[] = [bookmark]) {
  const onDelete = vi.fn();
  const onUpdate = vi.fn();
  render(<BookmarkList bookmarks={bookmarks} onDelete={onDelete} onUpdate={onUpdate} />);
  return { onDelete, onUpdate };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("BookmarkList", () => {
  it("shows an empty state", () => {
    renderList([]);
    expect(screen.getByText("아직 북마크가 없습니다")).toBeInTheDocument();
  });

  it("renders title as a link that opens in a new tab", () => {
    renderList();
    const link = screen.getByRole("link", { name: "FastAPI Docs" });
    expect(link).toHaveAttribute("href", "https://fastapi.tiangolo.com");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("renders tags and created date", () => {
    renderList();
    const item = screen.getByRole("listitem");
    expect(within(item).getByText("python")).toBeInTheDocument();
    expect(within(item).getByText("docs")).toBeInTheDocument();
    expect(within(item).getByText("2026-10-20")).toBeInTheDocument();
  });

  it("deletes after confirmation", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { onDelete } = renderList();
    await userEvent.click(screen.getByRole("button", { name: "삭제" }));
    expect(onDelete).toHaveBeenCalledWith(1);
  });

  it("does not delete when confirmation is cancelled", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const { onDelete } = renderList();
    await userEvent.click(screen.getByRole("button", { name: "삭제" }));
    expect(onDelete).not.toHaveBeenCalled();
  });

  it("edits a bookmark inline", async () => {
    const user = userEvent.setup();
    const { onUpdate } = renderList();
    await user.click(screen.getByRole("button", { name: "수정" }));
    const title = screen.getByLabelText("제목");
    await user.clear(title);
    await user.type(title, "Renamed");
    await user.click(screen.getByRole("button", { name: "저장" }));
    expect(onUpdate).toHaveBeenCalledWith(1, {
      url: "https://fastapi.tiangolo.com",
      title: "Renamed",
      tags: ["python", "docs"],
    });
  });
});
