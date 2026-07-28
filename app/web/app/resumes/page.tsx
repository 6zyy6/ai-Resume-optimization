import Link from "next/link";

import { Page } from "../../components/Page";
import { StatusTag } from "../../components/ui/StatusTag";

export default function ResumesPage() {
  return (
    <Page actions={<Link className="button button--primary" href="/create">新建简历</Link>} eyebrow="我的简历" title="基础版本与岗位版本">
      <section className="resume-list">
        <article className="resume-row"><div><StatusTag tone="success">基础简历</StatusTag><h2>产品运营实习</h2><p>最近更新：今天</p></div><div className="button-row"><Link href="/resumes/resume_sample_01/edit">编辑</Link><Link href="/resumes/resume_sample_01/versions">历史版本</Link><Link href="/jobs/new">岗位优化</Link></div></article>
        <article className="resume-row"><div><StatusTag tone="info">岗位版本</StatusTag><h2>内容运营 · 某教育产品</h2><p>基础版本保持不变</p></div><div className="button-row"><Link href="/resumes/resume_targeted_01/edit">编辑</Link><Link href="/exports/new?version=version_targeted_01">导出</Link></div></article>
      </section>
    </Page>
  );
}
