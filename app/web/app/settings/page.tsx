import { Page } from "../../components/Page";
import { Button } from "../../components/ui/Button";
import { StatusTag } from "../../components/ui/StatusTag";
export default function SettingsPage(){return <Page eyebrow="账号、隐私与数据" title="个人设置"><div className="settings-list"><section className="panel"><h2>账号绑定</h2><p>已验证邮箱用于 Web 登录和跨端同步。</p><StatusTag tone="success">邮箱已验证</StatusTag></section><section className="panel"><h2>数据副本</h2><p>请求一份账号、简历和事实记录的数据副本。</p><Button variant="secondary">申请数据导出</Button></section><section className="panel panel--danger"><h2>删除账号与数据</h2><p>删除请求进入异步任务；执行前会再次确认。</p><Button state="error" variant="secondary">申请删除</Button></section></div></Page>}
