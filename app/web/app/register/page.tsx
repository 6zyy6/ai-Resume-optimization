"use client";

import { ApiError } from "@resume/shared/client";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { type ChangeEvent, type FormEvent, Suspense, useEffect, useState } from "react";

import { Button } from "../../components/ui/Button";
import { Field } from "../../components/ui/Field";
import { createWebApiClient } from "../../features/api/client";
import { safeReturnTo } from "../../features/session/return-to";

function registerErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "AUTH_ACCOUNT_EXISTS") return "这个邮箱已经设置过密码，请返回登录。";
    if (error.code === "AUTH_CODE_INVALID") return "验证码无效或已过期，请重新发送。";
    if (error.code === "AUTH_RATE_LIMITED") return "请求次数过多，请按提示稍后重试。";
    if (error.code === "CONSENT_REQUIRED") return "请先阅读并同意当前版本的用户协议与隐私政策。";
    if (error.code === "AUTH_PROVIDER_UNAVAILABLE") return "注册服务暂时不可用，请稍后重试。";
  }
  return "注册没有完成，请检查填写内容后重试。";
}

function RegisterForm() {
  const search = useSearchParams();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [consented, setConsented] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const [sending, setSending] = useState(false);
  const [registering, setRegistering] = useState(false);
  const [error, setError] = useState("");
  const returnTo = safeReturnTo(search.get("returnTo"));
  const loginHref = `/login?returnTo=${encodeURIComponent(returnTo)}`;
  const passwordsDiffer = confirmation.length > 0 && password !== confirmation;

  useEffect(() => {
    if (cooldown === 0) return;
    const timer = window.setInterval(() => {
      setCooldown((current) => Math.max(0, current - 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [cooldown]);

  const sendCode = async () => {
    setSending(true);
    setError("");
    try {
      await createWebApiClient().post(
        "/v1/auth/email/start",
        { email },
        crypto.randomUUID(),
      );
      setCooldown(60);
    } catch (requestError) {
      setError(registerErrorMessage(requestError));
    } finally {
      setSending(false);
    }
  };

  const register = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (password !== confirmation) {
      setError("两次输入的密码不一致，请重新确认。");
      return;
    }
    setRegistering(true);
    setError("");
    try {
      await createWebApiClient().post(
        "/v1/auth/password/register",
        {
          email,
          code,
          password,
          consents: [
            { document_type: "user_agreement", document_version: "2026-07-27", decision: "accepted" },
            { document_type: "privacy_policy", document_version: "2026-07-27", decision: "accepted" },
          ],
        },
        crypto.randomUUID(),
      );
      window.location.assign(returnTo);
    } catch (requestError) {
      setError(registerErrorMessage(requestError));
    } finally {
      setRegistering(false);
    }
  };

  return (
    <main className="login-layout">
      <section className="login-copy">
        <Link className="wordmark" href="/">简历证据台</Link>
        <div className="login-copy__body">
          <p className="eyebrow">创建账号</p>
          <h1>保存每一次修改。</h1>
          <p>先确认邮箱归属，再设置密码。你的简历版本和事实证据会跟随账号保存。</p>
        </div>
      </section>
      <form className="form-card auth-form" onSubmit={register}>
        <header className="auth-form__head">
          <div>
            <h2>注册邮箱账号</h2>
            <p>已经有账号？ <Link href={loginHref}>返回登录</Link></p>
          </div>
        </header>

        <div className="auth-code-fields">
          <Field
            autoComplete="email"
            label="邮箱账号"
            name="register-email"
            onChange={(event: ChangeEvent<HTMLInputElement>) => setEmail(event.currentTarget.value)}
            placeholder="name@example.com"
            required
            type="email"
            value={email}
          />
          <Button
            disabled={!email || cooldown > 0}
            onClick={() => void sendCode()}
            state={sending ? "loading" : "default"}
            type="button"
            variant="secondary"
          >
            {cooldown > 0 ? `${cooldown} 秒后重发` : "发送验证码"}
          </Button>
        </div>
        <Field
          autoComplete="one-time-code"
          helper="验证码有效 10 分钟。"
          inputMode="numeric"
          label="6 位验证码"
          maxLength={6}
          name="register-code"
          onChange={(event: ChangeEvent<HTMLInputElement>) => setCode(event.currentTarget.value)}
          pattern="[0-9]{6}"
          required
          value={code}
        />
        <Field
          autoComplete="new-password"
          helper="至少 8 个字符。"
          label="设置密码"
          minLength={8}
          name="register-password"
          onChange={(event: ChangeEvent<HTMLInputElement>) => setPassword(event.currentTarget.value)}
          required
          type="password"
          value={password}
        />
        <Field
          autoComplete="new-password"
          error={passwordsDiffer ? "两次输入的密码不一致，请重新确认。" : undefined}
          label="确认密码"
          minLength={8}
          name="register-confirmation"
          onChange={(event: ChangeEvent<HTMLInputElement>) => setConfirmation(event.currentTarget.value)}
          required
          type="password"
          value={confirmation}
        />

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
          disabled={
            !email
            || code.length !== 6
            || password.length < 8
            || confirmation.length < 8
            || passwordsDiffer
            || !consented
          }
          state={registering ? "loading" : error ? "error" : "default"}
          type="submit"
        >
          注册并登录
        </Button>
        {error ? <p className="auth-error" role="alert">{error}</p> : null}
      </form>
    </main>
  );
}

export default function RegisterPage() {
  return <Suspense><RegisterForm /></Suspense>;
}
