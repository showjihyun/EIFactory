import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { LoginForm } from "./LoginForm";

async function submit(username: string, password: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("아이디"), username);
  await user.type(screen.getByLabelText("비밀번호"), password);
  await user.click(screen.getByRole("button", { name: "로그인" }));
}

describe("LoginForm", () => {
  it("passes credentials to onLogin", async () => {
    const onLogin = vi.fn().mockResolvedValue(undefined);
    render(<LoginForm onLogin={onLogin} />);
    await submit("demo", "secret");
    expect(onLogin).toHaveBeenCalledWith("demo", "secret");
  });

  it("shows an error when login fails", async () => {
    const onLogin = vi.fn().mockRejectedValue(new Error("401"));
    render(<LoginForm onLogin={onLogin} />);
    await submit("demo", "wrong");
    expect(await screen.findByText("로그인에 실패했습니다")).toBeInTheDocument();
  });
});
