import { Button, Text, View } from "@tarojs/components";
import Taro from "@tarojs/taro";
import { useState } from "react";
import { loginFromUserAction, logout } from "../../platform/auth";
import { PrimaryAction } from "../../components/ui/PrimaryAction";

export default function MePage() {
  const [status, setStatus] = useState<"idle" | "loading" | "success" | "error">("idle");
  const login = async () => {
    setStatus("loading");
    try {
      await loginFromUserAction();
      setStatus("success");
    } catch {
      setStatus("error");
    }
  };
  return (
    <View className="screen">
      <View className="page-head">
        <Text className="page-head__title">账户与数据</Text>
        <Text className="page-head__lede">登录只在你主动点击后发起，不保存长期令牌。</Text>
      </View>
      <View className="list">
        <View className="list-row" onClick={() => Taro.navigateTo({ url: "/subpackages/settings/privacy" })}>
          <Text className="stage__title">隐私与数据</Text><Text className="meta">导出副本或申请删除</Text>
        </View>
      </View>
      <View className="fixed-actions">
        <Button className="secondary-action" onClick={() => void logout()}>退出</Button>
        <PrimaryAction label={status === "success" ? "已登录" : "微信登录"} state={status === "idle" ? "default" : status} onClick={() => void login()} />
      </View>
    </View>
  );
}
