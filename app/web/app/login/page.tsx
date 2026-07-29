"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { type ChangeEvent, type FormEvent, Suspense, useState } from "react";

import { Button } from "../../components/ui/Button";
import { Field } from "../../components/ui/Field";
import { safeReturnTo } from "../../features/session/return-to";
import { createWebApiClient } from "../../features/api/client";

function LoginForm() {
  const search = useSearchParams();
  const [sent, setSent] = useState(false);
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const returnTo = safeReturnTo(search.get("returnTo"));
  const sendCode = async () => {
    setLoading(true);
    setError("");
    try {
      await createWebApiClient().post("/v1/auth/email/start", { email }, crypto.randomUUID());
      setSent(true);
    } catch {
      setError("验证码发送失败，请稍后重试。");
    } finally {
      setLoading(false);
    }
  };
  const verify = async () => {
    setLoading(true);
    setError("");
    try {
      await createWebApiClient().post("/v1/auth/email/verify", {
        email,
        code,
        consents: [
          { document_type: "user_agreement", document_version: "v1", decision: "accepted" },
          { document_type: "privacy_policy", document_version: "v1", decision: "accepted" },
        ],
      }, crypto.randomUUID());
      window.location.assign(returnTo);
    } catch {
      setError("验证码无效或已过期，请重新发送。");
    } finally {
      setLoading(false);
    }
  };
  return (
    <main className="login-layout">
      <section className="login-copy">
        <Link className="wordmark" href="/">简历证据台</Link>
        <p className="eyebrow">安全登录</p>
        <h1>继续你的简历。</h1>
        <p>验证码有效 10 分钟。同一邮箱发送后需等待 60 秒。</p>
      </section>
      <form className="form-card" onSubmit={(event: FormEvent<HTMLFormElement>) => { event.preventDefault(); void (sent ? verify() : sendCode()); }}>
        <Field label="邮箱" name="email" onChange={(event: ChangeEvent<HTMLInputElement>) => setEmail(event.currentTarget.value)} placeholder="name@example.com" required type="email" value={email} />
        {sent ? <Field inputMode="numeric" label="6 位验证码" maxLength={6} name="code" onChange={(event: ChangeEvent<HTMLInputElement>) => setCode(event.currentTarget.value)} required value={code} /> : null}
        <label className="check-row"><input required type="checkbox" /> <span>我已阅读并同意<Link href="/settings">用户协议与隐私政策</Link></span></label>
        {sent
          ? <Button state={loading ? "loading" : error ? "error" : "default"} type="submit">验证并登录</Button>
          : <Button disabled={!email} onClick={() => void sendCode()} state={loading ? "loading" : error ? "error" : "default"} type="button">发送验证码</Button>}
        {error ? <p role="alert">{error}</p> : null}
      </form>
    </main>
  );
}

export default function LoginPage() {
  return <Suspense><LoginForm /></Suspense>;
}
