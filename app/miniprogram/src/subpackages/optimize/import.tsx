import { Button, Text, View } from "@tarojs/components";
import Taro from "@tarojs/taro";
import { useState } from "react";
import type { components } from "@resume/shared/schema";
import { PrimaryAction } from "../../components/ui/PrimaryAction";
import { chooseResumeFile, inspectFile, uploadBinary } from "../../platform/files";
import { newIdempotencyKey, write } from "../../platform/request";

type Upload = components["schemas"]["UploadTokenResponse"];
type Import = components["schemas"]["ImportResponse"];

const mimeByExtension: Record<string, string> = {
  pdf: "application/pdf",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  txt: "text/plain",
};

export default function ImportPage() {
  const [message, setMessage] = useState("支持 PDF、DOCX、TXT，单文件不超过 10MiB。");
  const [state, setState] = useState<"default" | "loading" | "success" | "error">("default");
  const start = async () => {
    setState("loading");
    try {
      const selected = (await chooseResumeFile()).tempFiles[0];
      const extension = selected.name.split(".").pop()?.toLowerCase() ?? "";
      const mime = mimeByExtension[extension];
      if (!mime) throw new Error("unsupported");
      const metadata = await inspectFile(selected.path);
      const token = await write<components["schemas"]["UploadTokenRequest"], Upload>(
        "post", "/v1/files/upload-tokens",
        { display_name: selected.name, mime, purpose: "resume_import", ...metadata },
        newIdempotencyKey("upload-token"),
      );
      await uploadBinary(selected.path, { uploadUrl: token.upload_url, mimeType: mime });
      await write("post", `/v1/files/${token.file_id}/confirm-upload`, {}, newIdempotencyKey("confirm-upload"));
      const created = await write<components["schemas"]["ImportCreate"], Import>(
        "post", "/v1/imports", { file_id: token.file_id }, newIdempotencyKey("import"),
      );
      setMessage(created.task_id ? "正在解析，完成后会生成待确认事实。" : "导入已建立。");
      setState("success");
    } catch {
      setMessage("导入失败。请确认文件类型、大小和网络后重试，也可以选择从零填写。");
      setState("error");
    }
  };
  return (
    <View className="screen">
      <View className="page-head"><Text className="page-head__title">从哪份已有简历开始？</Text><Text className="page-head__lede">{message}</Text></View>
      <View className="panel"><Text>不会执行 OCR；扫描版 PDF 请先转换为可复制文字。</Text></View>
      <View className="fixed-actions">
        <Button className="secondary-action" onClick={() => Taro.navigateTo({ url: "/subpackages/create/index" })}>从零填写</Button>
        <PrimaryAction label="选择文件" state={state} onClick={() => void start()} />
      </View>
    </View>
  );
}
