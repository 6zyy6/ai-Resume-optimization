import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import LoginPage from "../app/login/page";
import PrivacyPolicyPage from "../app/legal/privacy-policy/page";
import UserAgreementPage from "../app/legal/user-agreement/page";
import RegisterPage from "../app/register/page";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));

afterEach(cleanup);

describe("email account pages", () => {
  it("defaults to email and password login with a visible registration route", () => {
    render(<LoginPage />);

    expect(screen.getByRole("textbox", { name: "邮箱账号" })).toBeInTheDocument();
    expect(screen.getByLabelText("密码")).toHaveAttribute("type", "password");
    expect(screen.getByRole("button", { name: "登录" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "注册账号" })).toHaveAttribute(
      "href",
      "/register?returnTo=%2Fhome",
    );
    expect(screen.getByRole("link", { name: "用户协议" })).toHaveAttribute(
      "href",
      "/legal/user-agreement",
    );
    expect(screen.getByRole("link", { name: "隐私政策" })).toHaveAttribute(
      "href",
      "/legal/privacy-policy",
    );
  });

  it("keeps email verification-code login as a fallback", () => {
    render(<LoginPage />);

    fireEvent.click(screen.getByRole("button", { name: "使用验证码登录" }));

    expect(screen.queryByLabelText("密码")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送验证码" })).toBeInTheDocument();
  });

  it("shows a complete email verification and password registration form", () => {
    render(<RegisterPage />);

    expect(screen.getByRole("textbox", { name: "邮箱账号" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "6 位验证码" })).toBeInTheDocument();
    expect(screen.getByLabelText("设置密码")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("确认密码")).toHaveAttribute("type", "password");
    expect(screen.getByRole("button", { name: "发送验证码" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "注册并登录" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回登录" })).toHaveAttribute(
      "href",
      "/login?returnTo=%2Fhome",
    );
  });

  it("provides independently accessible, versioned legal documents", () => {
    const agreement = render(<UserAgreementPage />);
    expect(screen.getByRole("heading", { name: "用户协议" })).toBeInTheDocument();
    expect(screen.getByText(/版本：2026-07-27/)).toBeInTheDocument();
    agreement.unmount();

    render(<PrivacyPolicyPage />);
    expect(screen.getByRole("heading", { name: "隐私政策" })).toBeInTheDocument();
    expect(screen.getByText(/数据导出与删除/)).toBeInTheDocument();
  });
});
