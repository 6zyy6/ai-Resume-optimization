import { Page } from "../../components/Page";
import { Button } from "../../components/ui/Button";
import { StatusTag } from "../../components/ui/StatusTag";
export default function FactsPage(){return <Page actions={<Button>新增事实</Button>} eyebrow="经历事实库" title="面试时能解释的内容"><section className="filter-row" aria-label="事实分类">{["教育","项目","实习","校园","竞赛","兼职","志愿","技能与荣誉"].map(x=><Button key={x} variant="quiet">{x}</Button>)}</section><article className="resume-row"><div><StatusTag tone="success">confirmed · 有来源</StatusTag><h2>课程产品设计</h2><p>设计访谈提纲并整理访谈结论。</p></div><Button variant="secondary">查看来源</Button></article></Page>}
