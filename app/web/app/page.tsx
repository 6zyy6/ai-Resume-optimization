import Link from "next/link";

export default function MarketingPage() {
  return (
    <main className="marketing">
      <header className="marketing-nav">
        <Link className="wordmark" href="/">简历证据台</Link>
        <Link className="command-pill" href="/login">登录 · ⌘ K</Link>
      </header>
      <section className="marketing-intro">
        <p className="eyebrow">大学生简历工作台</p>
        <h1>先找到真实经历，<br />再写成能被追问的简历。</h1>
        <p>从零梳理经历，或对照岗位逐条优化。建议必须引用你的事实，不把“不知道”改写成肯定。</p>
      </section>
      <section className="entry-workbench" aria-label="选择开始方式">
        <Link className="entry-panel" href="/create">
          <span>01 · 从零创建</span>
          <strong>我还没有简历</strong>
          <p>用场景问题找出课程、项目、社团和兼职中的有效经历。</p>
          <b>开始梳理 →</b>
        </Link>
        <Link className="entry-panel" href="/imports/new/confirm">
          <span>02 · 岗位优化</span>
          <strong>我已经有简历</strong>
          <p>导入文件、确认事实，再按目标岗位查看有证据的修改建议。</p>
          <b>导入简历 →</b>
        </Link>
      </section>
      <aside className="fact-note">
        <span aria-hidden="true">✓</span>
        <div><strong>事实保护不是一句提示语。</strong><p>未确认信息不会进入事实库；没有证据的岗位缺口会明确标为“真实缺口”。</p></div>
      </aside>
      <footer className="statement-footer">
        <p>写得更好，但不写成另一个人。</p>
        <Link href="/settings">隐私与数据</Link>
      </footer>
    </main>
  );
}
