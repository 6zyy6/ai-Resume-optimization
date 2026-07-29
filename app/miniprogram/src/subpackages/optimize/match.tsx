import { Input, Text, View } from "@tarojs/components";
import Taro, { useLoad } from "@tarojs/taro";
import { useState } from "react";
import type { components } from "@resume/shared/schema";
import { waitForTask } from "@resume/shared/workflows";
import { PrimaryAction } from "../../components/ui/PrimaryAction";
import { api, newIdempotencyKey, write } from "../../platform/request";

type Match = components["schemas"]["MatchResponse"];

export default function MatchPage() {
  const [jobId, setJobId] = useState("");
  const [resumeVersionId, setResumeVersionId] = useState("");
  const [message, setMessage] = useState("填写要用于匹配的简历版本 ID。");
  useLoad(({ jobId: id }) => setJobId(String(id ?? "")));
  const run = async () => {
    try {
      const match = await write<components["schemas"]["MatchCreate"], Match>(
        "post", "/v1/match-analyses",
        { job_id: jobId, resume_version_id: resumeVersionId.trim() },
        newIdempotencyKey("match"),
      );
      if (!match.task_id) throw new Error("匹配任务未创建");
      await waitForTask(() => api.get<components["schemas"]["TaskResponse"]>(`/v1/tasks/${match.task_id}`), match.task_id);
      await Taro.redirectTo({ url: `/subpackages/optimize/suggestions?analysisId=${match.id}` });
    } catch {
      setMessage("暂时无法发起匹配，请确认岗位解析完成且版本 ID 正确。");
    }
  };
  return (
    <View className="screen">
      <View className="page-head"><Text className="page-head__title">用哪个版本对齐岗位？</Text><Text className="page-head__lede">{message}</Text></View>
      <View className="field"><Text className="field__label">简历版本 ID</Text><Input className="field__control" value={resumeVersionId} onInput={(event) => setResumeVersionId(event.detail.value)} /></View>
      <View className="fixed-actions"><PrimaryAction label="开始匹配" disabled={!resumeVersionId.trim()} onClick={() => void run()} /></View>
    </View>
  );
}
