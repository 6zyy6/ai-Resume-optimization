"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { Button } from "../../components/ui/Button";
import { Field } from "../../components/ui/Field";
import { safeReturnTo } from "../../features/session/return-to";

function LoginForm() {
  const search = useSearchParams();
  const [sent, setSent] = useState(false);
  const returnTo = safeReturnTo(search.get("returnTo"));
  return (
    <main className="login-layout">
      <section className="login-copy">
        <Link className="wordmark" href="/">简历证据台</Link>
        <p className="eyebrow">安全登录</p>
        <h1>继续你的简历。</h1>
        <p>验证码有效 10 分钟。同一邮箱发送后需等待 60 秒。</p>
      </section>
      <form className="form-card" onSubmit={(event) => { event.preventDefault(); window.location.assign(returnTo); }}>
        <Field label="邮箱" name="email" placeholder="name@example.com" required type="email" />
        {sent ? <Field inputMode="numeric" label="6 位验证码" maxLength={6} name="code" required /> : null}
        <label className="check-row"><input required type="checkbox" /> <span>我已阅读并同意<Link href="/settings">用户协议与隐私政策</Link></span></label>
        {sent
          ? <Button type="submit">验证并登录</Button>
          : <Button onClick={() => setSent(true)} type="button">发送验证码</Button>}
      </form>
    </main>
  );
}

export default function LoginPage() {
  return <Suspense><LoginForm /></Suspense>;
}
