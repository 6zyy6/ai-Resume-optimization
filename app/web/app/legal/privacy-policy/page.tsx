import Link from "next/link";

export default function PrivacyPolicyPage() {
  return (
    <main className="legal-page">
      <header>
        <Link className="wordmark" href="/">简历证据台</Link>
        <p className="eyebrow">版本：2026-07-27</p>
        <h1>隐私政策</h1>
        <p>生效日期：2026 年 7 月 27 日</p>
      </header>
      <article className="legal-document">
        <section>
          <h2>1. 我们处理的信息</h2>
          <p>我们处理账号邮箱、登录会话、你主动提交的经历与简历、岗位说明、上传文件，以及完成任务所需的操作和审计记录。</p>
        </section>
        <section>
          <h2>2. 使用目的</h2>
          <p>信息用于身份验证、保存事实与版本、解析文件、执行 AI 工作流、导出简历、保障安全和处理你的隐私请求。</p>
        </section>
        <section>
          <h2>3. 保存期限</h2>
          <p>上传源文件在解析确认后最长保留 24 小时，导出 PDF 保留 7 天；业务事实和简历版本按账号生命周期保存，除非法律另有要求。</p>
        </section>
        <section>
          <h2>4. 数据安全</h2>
          <p>生产环境使用访问控制、传输加密、最小权限与审计措施。系统不持久化完整模型思考过程、访问令牌或密钥。</p>
        </section>
        <section>
          <h2>5. 数据导出与删除</h2>
          <p>你可以在账号与数据页面申请数据副本或删除账号。请求会进入可追踪的异步任务，完成状态可在任务中心查看。</p>
        </section>
      </article>
      <nav className="legal-links" aria-label="法律文档">
        <Link href="/legal/user-agreement">查看用户协议</Link>
        <Link href="/login">返回登录</Link>
      </nav>
    </main>
  );
}
