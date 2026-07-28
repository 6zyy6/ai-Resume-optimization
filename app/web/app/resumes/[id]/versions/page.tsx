import { Page } from "../../../../components/Page";
import { Button } from "../../../../components/ui/Button";
import { StatusTag } from "../../../../components/ui/StatusTag";

export default function VersionsPage() {
  return (
    <Page eyebrow="不可变历史" title="版本记录">
      <ol className="timeline">
        <li><StatusTag tone="success">当前版本</StatusTag><div><h2>v3 · 修改项目要点</h2><p>今天 14:32 · 从 v2 创建</p></div><Button variant="secondary">查看</Button></li>
        <li><StatusTag tone="info">历史版本</StatusTag><div><h2>v2 · 确认教育信息</h2><p>今天 13:18 · 历史快照不会被覆盖</p></div><Button variant="quiet">恢复为新版本</Button></li>
      </ol>
    </Page>
  );
}
