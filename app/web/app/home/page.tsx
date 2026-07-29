import Link from "next/link";

import { Page } from "../../components/Page";

export default function HomePage() {
  return (
    <Page eyebrow="今天的工作台" title="先完成一份能投递的版本。">
      <section className="action-grid">
        <Link className="action-card action-card--accent" href="/create"><span>从真实经历开始</span><strong>新建基础简历</strong><b>开始 →</b></Link>
        <Link className="action-card" href="/resumes?import=1"><span>已有文件</span><strong>导入并优化</strong><b>选择文件 →</b></Link>
      </section>
      <section className="dashboard-grid">
        <div className="panel panel--wide"><header><h2>最近简历</h2><Link href="/resumes">查看全部</Link></header>
          <p>创建或导入第一份简历后，这里会显示你自己的版本。</p>
        </div>
        <div className="panel"><h2>任务</h2><p>任务进度和失败原因会在任务页显示。</p><Link href="/tasks">查看任务</Link></div>
        <div className="panel"><h2>今日 AI 用量</h2><p>登录后可查看当天的实际用量。</p><Link href="/settings">账户设置</Link></div>
      </section>
    </Page>
  );
}
