import Link from "next/link";

import { Page } from "../../components/Page";
import { StatusTag } from "../../components/ui/StatusTag";

export default function HomePage() {
  return (
    <Page eyebrow="今天的工作台" title="先完成一份能投递的版本。">
      <section className="action-grid">
        <Link className="action-card action-card--accent" href="/create"><span>从真实经历开始</span><strong>新建基础简历</strong><b>开始 →</b></Link>
        <Link className="action-card" href="/resumes?import=1"><span>已有文件</span><strong>导入并优化</strong><b>选择文件 →</b></Link>
      </section>
      <section className="dashboard-grid">
        <div className="panel panel--wide"><header><h2>最近简历</h2><Link href="/resumes">查看全部</Link></header>
          <ul className="list"><li><Link href="/resumes/resume_sample_01/edit"><strong>产品运营实习 · 基础版</strong><span>今天更新</span></Link></li></ul>
        </div>
        <div className="panel"><h2>任务</h2><StatusTag tone="pending">匹配分析排队中</StatusTag><p>失败任务会保留输入，并提供明确重试入口。</p></div>
        <div className="panel"><h2>今日 AI 用量</h2><p className="usage">0 / 20</p><p>编辑、预览和查看历史不受限。</p></div>
      </section>
    </Page>
  );
}
