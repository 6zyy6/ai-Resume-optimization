"use client";

import { ApiError } from "@resume/shared/client";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { type ChangeEvent, type FormEvent, Suspense, useState } from "react";

import { Button } from "../../components/ui/Button";
import { Field } from "../../components/ui/Field";
import { createWebApiClient } from "../../features/api/client";
import { safeReturnTo } from "../../features/session/return-to";

type LoginMode = "password" | "otp";

function loginErrorMessage(error: unknown, mode: LoginMode): string {
  if (error instanceof ApiError) {
    if (error.code === "AUTH_RATE_LIMITED") return "尝试次数过多，请稍后再试。";
    if (error.code === "CONSENT_REQUIRED") return "请先阅读并同意当前版本的用户协议与隐私政策。";
    if (error.code === "AUTH_PROVIDER_UNAVAILABLE") return "登录服务暂时不可用，请稍后重试。";
  }
  return mode === "password"
    ? "邮箱或密码不正确；未设置密码的账号可改用验证码登录。"
    : "验证码无效或已过期，请重新发送。";
}

function LoginForm() {
  const search = useSearchParams();
  const [mode, setMode] = useState<LoginMode>("password");
  const [sent, setSent] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [consented, setConsented] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const returnTo = safeReturnTo(search.get("returnTo"));
  const registerHref = `/register?returnTo=${encodeURIComponent(returnTo)}`;
  const consents = [
    { document_type: "user_agreement", document_version: "2026-07-27", decision: "accepted" },
    { document_type: "privacy_policy", document_version: "2026-07-27", decision: "accepted" },
  ] as const;

  const sendCode = async () => {
    setLoading(true);
    setError("");
    try {
      await createWebApiClient().post(
        "/v1/auth/email/start",
        { email },
        crypto.randomUUID(),
      );
      setSent(true);
    } catch (requestError) {
      setError(loginErrorMessage(requestError, "otp"));
    } finally {
      setLoading(false);
    }
  };

  const login = async () => {
    setLoading(true);
    setError("");
    try {
      if (mode === "password") {
        await createWebApiClient().post(
          "/v1/auth/password/login",
          { email, password, consents },
          crypto.randomUUID(),
        );
      } else {
        await createWebApiClient().post(
          "/v1/auth/email/verify",
          { email, code, consents },
          crypto.randomUUID(),
        );
      }
      window.location.assign(returnTo);
    } catch (requestError) {
      setError(loginErrorMessage(requestError, mode));
    } finally {
      setLoading(false);
    }
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void (mode === "otp" && !sent ? sendCode() : login());
  };

  const switchMode = () => {
    setMode((current) => current === "password" ? "otp" : "password");
    setError("");
  };

  return (
    <main className="login-layout">
      <section className="login-copy">
        <Link className="wordmark" href="/">简历证据台</Link>
        <div className="login-copy__body">
          <p className="eyebrow">账号登录</p>
          <h1>继续你的简历。</h1>
          <p>用邮箱账号同步简历、事实证据和岗位优化记录。</p>
        </div>
      </section>
      <form className="form-card auth-form" onSubmit={submit}>
        <header className="auth-form__head">
          <div>
            <h2>登录账号</h2>
            <p>还没有账号？ <Link href={registerHref}>注册账号</Link></p>
          </div>
          <Button
            className="auth-mode-switch"
            onClick={switchMode}
            type="button"
            variant="quiet"
          >
            {mode === "password" ? "使用验证码登录" : "使用密码登录"}
          </Button>
        </header>

        <Field
          autoComplete="email"
          label="邮箱账号"
          name="login-email"
          onChange={(event: ChangeEvent<HTMLInputElement>) => setEmail(event.currentTarget.value)}
          placeholder="name@example.com"
          required
          type="email"
          value={email}
        />
        {mode === "password" ? (
          <Field
            autoComplete="current-password"
            label="密码"
            minLength={8}
            name="login-password"
            onChange={(event: ChangeEvent<HTMLInputElement>) => setPassword(event.currentTarget.value)}
            required
            type="password"
            value={password}
          />
        ) : sent ? (
          <Field
            autoComplete="one-time-code"
            inputMode="numeric"
            label="6 位验证码"
            maxLength={6}
            name="login-code"
            onChange={(event: ChangeEvent<HTMLInputElement>) => setCode(event.currentTarget.value)}
            pattern="[0-9]{6}"
            required
            value={code}
          />
        ) : (
          <p className="auth-hint">验证码有效 10 分钟，同一邮箱发送后需等待 60 秒。</p>
        )}

        <label className="check-row">
          <input
            checked={consented}
            onChange={(event) => setConsented(event.currentTarget.checked)}
            required
            type="checkbox"
          />
          <span>我已阅读并同意 <Link href="/legal/user-agreement">用户协议</Link> 与 <Link href="/legal/privacy-policy">隐私政策</Link></span>
        </label>

        <Button
          className="auth-submit"
          disabled={!email || !consented || (mode === "password" ? password.length < 8 : sent && code.length !== 6)}
          state={loading ? "loading" : error ? "error" : "default"}
          type="submit"
        >
          {mode === "password" ? "登录" : sent ? "验证并登录" : "发送验证码"}
        </Button>
        {error ? <p className="auth-error" role="alert">{error}</p> : null}
      </form>
    </main>
  );
}

export default function LoginPage() {
  return <Suspense><LoginForm /></Suspense>;
}
