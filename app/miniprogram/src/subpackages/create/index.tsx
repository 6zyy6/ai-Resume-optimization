import { Input, Text, View } from "@tarojs/components";
import Taro from "@tarojs/taro";
import { useState } from "react";
import type { components } from "@resume/shared/schema";
import { PrimaryAction } from "../../components/ui/PrimaryAction";
import { newIdempotencyKey, write } from "../../platform/request";

type Resume = components["schemas"]["ResumeResponse"];

export default function CreatePage() {
  const [title, setTitle] = useState("我的基础简历");
  const [state, setState] = useState<"default" | "loading" | "error">("default");
  const create = async () => {
    setState("loading");
    try {
      const resume = await write<components["schemas"]["ResumeCreate"], Resume>(
        "post",
        "/v1/resumes",
        { kind: "base", title: title.trim() },
        newIdempotencyKey("create-resume"),
      );
      await Taro.redirectTo({ url: `/subpackages/resume/editor?resumeId=${resume.id}` });
    } catch {
      setState("error");
    }
  };
  return (
    <View className="screen">
      <View className="page-head">
        <Text className="page-head__title">这份简历叫什么？</Text>
        <Text className="page-head__lede">只用于你自己区分版本，之后可以修改。</Text>
      </View>
      <View className="field">
        <Text className="field__label">简历名称</Text>
        <Input className="field__control" maxlength={60} value={title} onInput={(event) => setTitle(event.detail.value)} />
        <Text className="helper">{state === "error" ? "创建失败，请检查名称和网络后重试。" : "例如：产品实习基础简历"}</Text>
      </View>
      <View className="fixed-actions">
        <PrimaryAction label="创建并填写" state={state} disabled={!title.trim()} onClick={() => void create()} />
      </View>
    </View>
  );
}
