"use client";

import { useRouter } from "next/navigation";
import { type ChangeEvent, useState } from "react";
import type { components } from "@resume/shared/schema";

import { Page } from "../../components/Page";
import { Button } from "../../components/ui/Button";
import { Field } from "../../components/ui/Field";
import { createWebApiClient } from "../../features/api/client";

const steps = ["求职方向", "教育信息", "经历雷达", "经历深挖", "确认事实", "生成草稿"];

export default function CreatePage() {
  const [step, setStep] = useState(0);
  const [answer, setAnswer] = useState("");
  const [saving, setSaving] = useState(false);
  const router = useRouter();
  async function continueFlow() {
    setSaving(true);
    const api = createWebApiClient();
    const fact: components["schemas"]["FactCreate"] = {
      kind: steps[step],
      sources: [{ content: answer || "跳过", source_type: "question_answer" }],
      status: "unconfirmed",
      value: answer || "跳过",
    };
    await api.post("/v1/facts", fact, crypto.randomUUID());
    if (step === steps.length - 1) {
      const resume: components["schemas"]["ResumeCreate"] = { kind: "base", title: "产品运营实习" };
      const created = await api.post<typeof resume, components["schemas"]["ResumeResponse"]>("/v1/resumes", resume, crypto.randomUUID());
      router.push(`/resumes/${created.id}/edit`);
    } else {
      setStep(step + 1);
    }
    setSaving(false);
  }
  return (
    <Page eyebrow={`步骤 ${step + 1} / ${steps.length} · 还剩 ${steps.length - step - 1} 个主要问题`} title="从一段真实经历开始">
      <div className="editor-grid wizard-grid">
        <aside className="editor-rail" aria-label="创建步骤">
          <ol>{steps.map((label, index) => <li aria-current={index === step ? "step" : undefined} key={label}>{index + 1}. {label}</li>)}</ol>
          <p>后退不会删除已确认的事实。</p>
        </aside>
        <section className="editor-main">
          <p className="eyebrow">经历雷达 · 确定性演示</p>
          <h2>最近一年，你完成过什么需要持续投入的任务？</h2>
          <Field
            helper={`${answer.length} / 1000 字`}
            label="你的回答"
            maxLength={1000}
            multiline
            name="experience"
            onChange={(event: ChangeEvent<HTMLTextAreaElement>) => setAnswer(event.currentTarget.value)}
            placeholder="例如：课程项目、社团活动、兼职或志愿服务"
            value={answer}
          />
          <div className="button-row">
            <Button onClick={() => void continueFlow()} state={saving ? "loading" : "default"}>确认并继续</Button>
            <Button onClick={() => { setAnswer(""); void continueFlow(); }} variant="quiet">跳过此题</Button>
          </div>
        </section>
        <aside className="editor-context">
          <p className="eyebrow">即时预览</p>
          <article className="resume-paper">
            <h3>项目经历</h3>
            <p>{answer || "回答后，这里会出现一条可编辑的简历表达。"}</p>
          </article>
          {step === steps.length - 1 ? <p>确认后将打开刚创建的草稿。</p> : null}
        </aside>
      </div>
    </Page>
  );
}
