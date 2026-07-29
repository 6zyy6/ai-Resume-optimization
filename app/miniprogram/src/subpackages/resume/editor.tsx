import { Button, Input, Text, Textarea, View } from "@tarojs/components";
import Taro, { useDidShow, useLoad } from "@tarojs/taro";
import { useEffect, useState } from "react";
import type { components } from "@resume/shared/schema";
import { claimEvidenceForText } from "@resume/shared/workflows";
import { PrimaryAction } from "../../components/ui/PrimaryAction";
import { loadDraft, registerLifecycleHooks, saveDraft, syncDraft, type LocalDraft } from "../../features/draft-store";
import { api, newIdempotencyKey, write } from "../../platform/request";

type Resume = components["schemas"]["ResumeResponse"];
type Version = components["schemas"]["ResumeVersionResponse"];
type DraftValue = { title: string; target: string; experience: string };

export default function EditorPage() {
  const [resumeId, setResumeId] = useState("");
  const [resume, setResume] = useState<Resume>();
  const [value, setValue] = useState<DraftValue>({ title: "", target: "", experience: "" });
  const [dirty, setDirty] = useState(false);
  const [state, setState] = useState<"default" | "loading" | "success" | "error">("default");
  useLoad(({ resumeId: id }) => setResumeId(String(id ?? "")));
  const refresh = async () => {
    if (!resumeId) return;
    const remote = await api.get<Resume>(`/v1/resumes/${resumeId}`);
    setResume(remote);
    const local = await loadDraft<DraftValue>(resumeId);
    if (local) setValue(local.value);
    else setValue((current) => ({ ...current, title: remote.title }));
  };
  useDidShow(() => void refresh());
  useEffect(() => registerLifecycleHooks({
    flush: async () => {
      if (dirty && resumeId) await saveDraft({ resumeId, updatedAt: Date.now(), value });
    },
    refresh,
  }), [dirty, resumeId, value]);
  const update = (patch: Partial<DraftValue>) => {
    setValue((current) => ({ ...current, ...patch }));
    setDirty(true);
  };
  const save = async () => {
    if (!resume) return;
    setState("loading");
    const draft: LocalDraft<DraftValue> = { resumeId, updatedAt: Date.now(), value };
    try {
      await saveDraft(draft);
      await syncDraft(draft, async ({ value: current }) => {
        const snapshot: components["schemas"]["ResumeSnapshot"] = {
          schema_version: "1",
          title: current.title,
          target: current.target || null,
          sections: current.experience.trim() ? [{
            id: "experience",
            title: "经历",
            type: "experience",
            items: [{ id: "experience-1", text: current.experience.trim(), fact_refs: [] }],
          }] : [],
        };
        const claim_evidence = [];
        if (current.experience.trim()) {
          const fact = await write<components["schemas"]["FactCreate"], components["schemas"]["FactResponse"]>(
            "post",
            "/v1/facts",
            { kind: "resume_bullet", value: current.experience.trim(), status: "confirmed", sources: [{ source_type: "user_edit", content: current.experience.trim() }] },
            newIdempotencyKey(`fact-${resumeId}`),
          );
          claim_evidence.push(...claimEvidenceForText("experience-1", current.experience.trim(), fact.id));
        }
        await write<components["schemas"]["VersionCreate"], Version>(
          "post",
          `/v1/resumes/${resumeId}/versions`,
          { base_version: resume.version, snapshot, claim_evidence },
          newIdempotencyKey(`save-version-${resumeId}`),
        );
      });
      setDirty(false);
      setState("success");
      await refresh();
    } catch {
      setState("error");
    }
  };
  return (
    <View className="screen">
      <View className="page-head">
        <Text className="page-head__title">先写清楚一段真实经历</Text>
        <Text className="page-head__lede">没有事实关联的声明可以保存，但不能通过导出检查。</Text>
      </View>
      <View className="field"><Text className="field__label">简历名称</Text><Input className="field__control" value={value.title} onInput={(e) => update({ title: e.detail.value })} /></View>
      <View className="field"><Text className="field__label">目标方向</Text><Input className="field__control" value={value.target} onInput={(e) => update({ target: e.detail.value })} /></View>
      <View className="field">
        <Text className="field__label">经历描述</Text>
        <Textarea className="field__control field__control--textarea" maxlength={2000} value={value.experience} onInput={(e) => update({ experience: e.detail.value })} />
        <Text className="helper">{state === "error" ? "保存失败；草稿仍保留在本机，可稍后重试。" : "写你做了什么、如何做、结果是什么。"}</Text>
      </View>
      <View className="fixed-actions">
        <Button className="secondary-action" onClick={() => Taro.navigateTo({ url: `/subpackages/resume/preview?resumeId=${resumeId}` })}>预览</Button>
        <PrimaryAction label={state === "success" ? "已保存" : "保存版本"} state={state} onClick={() => void save()} />
      </View>
    </View>
  );
}
