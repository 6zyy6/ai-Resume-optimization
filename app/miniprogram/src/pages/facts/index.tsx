import { Button, Text, View } from "@tarojs/components";
import { useState } from "react";
import Taro, { useDidShow } from "@tarojs/taro";
import type { components } from "@resume/shared/schema";
import { api, newIdempotencyKey, write } from "../../platform/request";
import { StatusTag } from "../../components/ui/StatusTag";

type Fact = components["schemas"]["FactResponse"];
type FactList = components["schemas"]["FactListResponse"];

export default function FactsPage() {
  const [items, setItems] = useState<Fact[]>([]);
  const refresh = async () => setItems((await api.get<FactList>("/v1/facts?limit=50")).items);
  useDidShow(() => void refresh());
  const decide = async (fact: Fact, decision: "confirm" | "reject") => {
    await write("post", `/v1/facts/${fact.id}/${decision}`, {}, newIdempotencyKey(`fact-${decision}`));
    await refresh();
  };
  return (
    <View className="screen">
      <View className="page-head">
        <Text className="page-head__title">哪些经历可以写进简历？</Text>
        <Text className="page-head__lede">确认只代表内容真实；有来源的已确认事实才可支持导出。</Text>
      </View>
      <View className="list">
        {items.map((fact) => (
          <View className="list-row" key={fact.id}>
            <View className="row"><Text>{fact.value}</Text><StatusTag status={fact.status} /></View>
            {fact.status === "unconfirmed" ? (
              <View className="row">
                <Button className="secondary-action" onClick={() => void decide(fact, "reject")}>不采用</Button>
                <Button className="primary-action" onClick={() => void decide(fact, "confirm")}>确认真实</Button>
              </View>
            ) : null}
          </View>
        ))}
      </View>
    </View>
  );
}
