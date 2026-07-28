import { Page } from "../../components/Page";
import { Button } from "../../components/ui/Button";
import { StatusTag } from "../../components/ui/StatusTag";
export default function TasksPage(){return <Page eyebrow="异步任务" title="任务中心"><section className="resume-list"><article className="resume-row"><div><StatusTag tone="pending">匹配分析 · 排队中</StatusTag><h2>内容运营岗位匹配</h2><p>输入已保存，可以离开此页面。</p></div><Button variant="quiet">取消任务</Button></article><article className="resume-row"><div><StatusTag tone="error">导出失败</StatusTag><h2>PDF 导出</h2><p>版本与模板选择已保留，可以重试。</p></div><Button state="error" variant="secondary">重新导出</Button></article></section></Page>}
