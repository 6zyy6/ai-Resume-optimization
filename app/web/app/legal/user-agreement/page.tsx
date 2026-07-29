import Link from "next/link";

export default function UserAgreementPage() {
  return (
    <main className="legal-page">
      <header>
        <Link className="wordmark" href="/">简历证据台</Link>
        <p className="eyebrow">版本：2026-07-27</p>
        <h1>用户协议</h1>
        <p>生效日期：2026 年 7 月 27 日</p>
      </header>
      <article className="legal-document">
        <section>
          <h2>1. 服务范围</h2>
          <p>本产品帮助你整理经历事实、创建简历版本、分析岗位要求并生成导出文件。你需要确保提交的信息真实、合法，并拥有必要的处理权限。</p>
        </section>
        <section>
          <h2>2. 账号安全</h2>
          <p>账号仅供本人使用。请妥善保管登录凭据；发现异常访问时，应及时退出会话并联系我们。</p>
        </section>
        <section>
          <h2>3. AI 生成内容</h2>
          <p>AI 输出是辅助草稿，不构成录用承诺。系统会通过事实来源限制无证据内容，但你仍需在投递前逐项检查。</p>
        </section>
        <section>
          <h2>4. 可接受使用</h2>
          <p>不得上传恶意文件、冒用他人资料、绕过访问控制，或利用服务实施违法活动。</p>
        </section>
        <section>
          <h2>5. 服务变更与终止</h2>
          <p>我们可能因维护、安全或合规要求调整服务。你可以在设置中申请数据副本或删除账号与数据。</p>
        </section>
      </article>
      <nav className="legal-links" aria-label="法律文档">
        <Link href="/legal/privacy-policy">查看隐私政策</Link>
        <Link href="/login">返回登录</Link>
      </nav>
    </main>
  );
}
