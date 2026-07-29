import { Button, Text, View } from "@tarojs/components";
import { useState } from "react";
import Taro, { useDidShow, useLoad } from "@tarojs/taro";
import type { components } from "@resume/shared/schema";
import { api, newIdempotencyKey, write } from "../../platform/request";
import { StatusTag } from "../../components/ui/StatusTag";

type Suggestion = components["schemas"]["SuggestionResponse"];
type Suggestions = components["schemas"]["SuggestionListResponse"];

export default function SuggestionsPage() {
  const [analysisId, setAnalysisId] = useState("");
  const [items, setItems] = useState<Suggestion[]>([]);
  const [sort, setSort] = useState<"risk" | "status">("risk");
  useLoad(({ analysisId: id }) => setAnalysisId(String(id ?? "")));
  const refresh = async () => {
    if (!analysisId) return;
    setItems((await api.get<Suggestions>(`/v1/match-analyses/${analysisId}/suggestions`)).items);
  };
  useDidShow(() => void refresh());
  const decide = async (suggestion: Suggestion, decision: "accept" | "ignore" | "revert") => {
    await write("post", `/v1/suggestions/${suggestion.id}/${decision}`, {}, newIdempotencyKey(`suggestion-${decision}`));
    await refresh();
  };
  const visible = [...items].sort((a, b) => sort === "risk"
    ? b.risk_flags.length - a.risk_flags.length
    : a.status.localeCompare(b.status));
  return (
    <View className="source-sheet">
      <View className="page-head"><Text className="page-head__title">逐条决定，不自动覆盖</Text><Text className="page-head__lede">每次接受、忽略或撤销都会创建新版本。</Text></View>
      <View className="sort-row">
        <Button className={`sort-button ${sort === "risk" ? "sort-button--active" : ""}`} onClick={() => setSort("risk")}>风险优先</Button>
        <Button className={`sort-button ${sort === "status" ? "sort-button--active" : ""}`} onClick={() => setSort("status")}>状态排序</Button>
      </View>
      <View className="list">
        {visible.map((item) => (
          <View className="suggestion" key={item.id}>
            <View className="row"><StatusTag status={item.status} /><Text className="meta">{item.risk_flags.length ? `${item.risk_flags.length} 项风险` : "未发现额外风险"}</Text></View>
            <Text className="meta">原文</Text><Text>{item.original_text}</Text>
            <Text className="meta">建议</Text><Text>{item.suggested_text}</Text>
            <Text className="meta">依据：{item.requirement_text ?? "表达清晰度"}</Text>
            <View className="row">
              <Button className="secondary-action" onClick={() => void decide(item, item.status === "ignored" ? "revert" : "ignore")}>{item.status === "ignored" ? "撤销" : "忽略"}</Button>
              <Button className="primary-action" onClick={() => void decide(item, "accept")}>接受建议</Button>
            </View>
          </View>
        ))}
      </View>
    </View>
  );
}
