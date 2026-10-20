import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { BookmarkForm } from "./BookmarkForm";

async function fill(url: string, title: string, tags = "") {
  const user = userEvent.setup();
  if (url) await user.type(screen.getByLabelText("URL"), url);
  if (title) await user.type(screen.getByLabelText("제목"), title);
  if (tags) await user.type(screen.getByLabelText("태그 (쉼표로 구분)"), tags);
  await user.click(screen.getByRole("button", { name: "추가" }));
}

describe("BookmarkForm", () => {
  it("submits normalized input", async () => {
    const onSubmit = vi.fn();
    render(<BookmarkForm onSubmit={onSubmit} />);
    await fill("https://example.com", "Example", "Docs, docs, React");
    expect(onSubmit).toHaveBeenCalledWith({
      url: "https://example.com",
      title: "Example",
      tags: ["docs", "react"],
    });
  });

  it("rejects a URL without http(s)", async () => {
    const onSubmit = vi.fn();
    render(<BookmarkForm onSubmit={onSubmit} />);
    await fill("example.com", "Example");
    expect(screen.getByText("URL은 http:// 또는 https://로 시작해야 합니다")).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("requires a title", async () => {
    const onSubmit = vi.fn();
    render(<BookmarkForm onSubmit={onSubmit} />);
    await fill("https://example.com", "");
    expect(screen.getByText("제목을 입력하세요")).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("clears the fields after a successful submit", async () => {
    render(<BookmarkForm onSubmit={vi.fn()} />);
    await fill("https://example.com", "Example");
    expect(screen.getByLabelText("URL")).toHaveValue("");
    expect(screen.getByLabelText("제목")).toHaveValue("");
  });

  it("prefills initial values when editing", () => {
    render(
      <BookmarkForm
        initial={{ url: "https://a.dev", title: "A", tags: ["x", "y"] }}
        submitLabel="저장"
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.getByLabelText("URL")).toHaveValue("https://a.dev");
    expect(screen.getByLabelText("태그 (쉼표로 구분)")).toHaveValue("x, y");
    expect(screen.getByRole("button", { name: "저장" })).toBeInTheDocument();
  });
});
