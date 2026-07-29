import { Input, Text, Textarea, View } from "@tarojs/components";
import Taro from "@tarojs/taro";
import { useState } from "react";
import type { components } from "@resume/shared/schema";
import { waitForTask } from "@resume/shared/workflows";
import { PrimaryAction } from "../../components/ui/PrimaryAction";
import { api, newIdempotencyKey, write } from "../../platform/request";

type Job = components["schemas"]["JobResponse"];

export default function JobPage() {
  const [title, setTitle] = useState("");
  const [raw, setRaw] = useState("");
  const [state, setState] = useState<"default" | "loading" | "error">("default");
  const submit = async () => {
    setState("loading");
    try {
      const job = await write<components["schemas"]["JobCreate"], Job>(
        "post", "/v1/jobs", { title, raw }, newIdempotencyKey("job"),
      );
      const parsing = await write<{}, Job>("post", `/v1/jobs/${job.id}/parse`, {}, newIdempotencyKey("parse-job"));
      if (!parsing.task_id) throw new Error("岗位解析任务未创建");
      await waitForTask(() => api.get<components["schemas"]["TaskResponse"]>(`/v1/tasks/${parsing.task_id}`), parsing.task_id);
      await Taro.redirectTo({ url: `/subpackages/optimize/match?jobId=${job.id}` });
    } catch {
      setState("error");
    }
  };
  return (
    <View className="screen">
      <View className="page-head"><Text className="page-head__title">你准备投什么岗位？</Text><Text className="page-head__lede">粘贴完整职位描述，系统会拆出可核对的岗位要求。</Text></View>
      <View className="field"><Text className="field__label">岗位名称</Text><Input className="field__control" value={title} onInput={(e) => setTitle(e.detail.value)} /></View>
      <View className="field"><Text className="field__label">职位描述</Text><Textarea className="field__control field__control--textarea" maxlength={10000} value={raw} onInput={(e) => setRaw(e.detail.value)} /></View>
      <View className="fixed-actions"><PrimaryAction label="解析岗位要求" state={state} disabled={!title.trim() || !raw.trim()} onClick={() => void submit()} /></View>
    </View>
  );
}
