import { Button, Text, View } from "@tarojs/components";
import { useState } from "react";
import { newIdempotencyKey, write } from "../../platform/request";
import { PrimaryAction } from "../../components/ui/PrimaryAction";

export default function PrivacyPage() {
  const [message, setMessage] = useState("数据副本和删除申请都在后台处理，可在任务中心查看进度。");
  const request = async (kind: "data-exports" | "deletion-requests") => {
    try {
      await write("post", `/v1/me/${kind}`, {}, newIdempotencyKey(kind));
      setMessage(kind === "data-exports" ? "数据副本申请已提交。" : "删除申请已提交，进入冷静期。");
    } catch {
      setMessage("申请未提交，请稍后重试。");
    }
  };
  return (
    <View className="screen">
      <View className="page-head"><Text className="page-head__title">你希望如何处理数据？</Text><Text className="page-head__lede">{message}</Text></View>
      <View className="stage"><Text className="stage__title">导出数据副本</Text><Text className="helper">生成你当前账户的可下载副本。</Text></View>
      <View className="stage"><Text className="stage__title">删除账户数据</Text><Text className="helper">提交后按隐私流程处理；这是高影响操作。</Text></View>
      <View className="fixed-actions">
        <Button className="secondary-action" onClick={() => void request("deletion-requests")}>申请删除</Button>
        <PrimaryAction label="导出数据" onClick={() => void request("data-exports")} />
      </View>
    </View>
  );
}
