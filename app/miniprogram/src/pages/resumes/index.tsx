import { Text, View } from "@tarojs/components";
import Taro, { useDidShow } from "@tarojs/taro";
import { useState } from "react";
import type { components } from "@resume/shared/schema";
import { api } from "../../platform/request";
import { PrimaryAction } from "../../components/ui/PrimaryAction";

type Resume = components["schemas"]["ResumeResponse"];
type ResumeList = components["schemas"]["ResumeListResponse"];

export default function ResumesPage() {
  const [items, setItems] = useState<Resume[]>([]);
  const [error, setError] = useState("");
  const refresh = async () => {
    try {
      const response = await api.get<ResumeList>("/v1/resumes?limit=20");
      setItems(response.items);
      setError("");
    } catch {
      setError("暂时无法读取简历，请检查网络后重试。");
    }
  };
  useDidShow(() => void refresh());
  return (
    <View className="screen">
      <View className="page-head">
        <Text className="page-head__title">我的简历</Text>
        <Text className="page-head__lede">基础简历保持不变，岗位版本单独保存。</Text>
      </View>
      {error ? <Text className="helper">{error}</Text> : null}
      <View className="list">
        {items.map((resume) => (
          <View className="list-row" key={resume.id} onClick={() => Taro.navigateTo({ url: `/subpackages/resume/editor?resumeId=${resume.id}` })}>
            <Text className="stage__title">{resume.title}</Text>
            <Text className="meta">版本 {resume.version}</Text>
          </View>
        ))}
        {!items.length && !error ? <Text className="helper">还没有简历，从一份基础简历开始。</Text> : null}
      </View>
      <View className="fixed-actions">
        <PrimaryAction label="新建简历" onClick={() => Taro.navigateTo({ url: "/subpackages/create/index" })} />
      </View>
    </View>
  );
}
