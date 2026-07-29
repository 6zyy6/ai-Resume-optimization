import { Button, Text, View, WebView } from "@tarojs/components";
import Taro, { useLoad } from "@tarojs/taro";
import { useState } from "react";
import type { components } from "@resume/shared/schema";
import { PrimaryAction } from "../../components/ui/PrimaryAction";
import { saveRemoteFile } from "../../platform/files";
import { api, newIdempotencyKey, write } from "../../platform/request";

type Versions = components["schemas"]["ResumeVersionsResponse"];
type ExportResult = components["schemas"]["ExportResponse"];

export default function PreviewPage() {
  const [resumeId, setResumeId] = useState("");
  const [result, setResult] = useState<ExportResult>();
  const [message, setMessage] = useState("远程生成后可在此预览 PDF。");
  useLoad(({ resumeId: id }) => setResumeId(String(id ?? "")));
  const createExport = async () => {
    const versions = await api.get<Versions>(`/v1/resumes/${resumeId}/versions?limit=1`);
    const version = versions.items[0];
    if (!version) return setMessage("请先保存至少一个版本。");
    const created = await write<components["schemas"]["ExportCreate"], ExportResult>(
      "post", "/v1/exports",
      { resume_version_id: version.id, template_version: "clear-standard" },
      newIdempotencyKey("export"),
    );
    setResult(created);
    setMessage(created.status === "succeeded" ? "导出已完成。" : "正在远程生成，可稍后刷新。");
  };
  const download = async () => {
    if (!result?.download_url) return setMessage("文件仍在生成，请稍后刷新。");
    const outcome = await saveRemoteFile(result.download_url);
    setMessage(outcome.saved ? "已保存到微信文件。" : outcome.alternative ?? "无法保存。");
  };
  return (
    <View className="screen screen--flush">
      {result?.download_url ? <WebView src={result.download_url} /> : <View className="source-sheet"><Text className="page-head__title">远程 PDF 预览</Text><Text className="page-head__lede">{message}</Text></View>}
      <View className="fixed-actions">
        <Button className="secondary-action" onClick={() => void download()}>保存文件</Button>
        <PrimaryAction label="生成预览" onClick={() => void createExport()} />
      </View>
    </View>
  );
}
