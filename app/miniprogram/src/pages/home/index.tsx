import { Text, View } from "@tarojs/components";
import Taro from "@tarojs/taro";
import { PrimaryAction } from "../../components/ui/PrimaryAction";

export default function HomePage() {
  return (
    <View className="screen">
      <View className="page-head">
        <Text className="page-head__title">今天先完成哪一步？</Text>
        <Text className="page-head__lede">事实由你确认，AI 只帮助整理和表达。</Text>
      </View>
      <View className="stage">
        <Text className="stage__number">1.0 · 建立底稿</Text>
        <Text className="stage__title">从零填写一份基础简历</Text>
        <Text className="helper">按模块保存，退出后可在 7 天内继续。</Text>
      </View>
      <View className="stage">
        <Text className="stage__number">2.0 · 导入经历</Text>
        <Text className="stage__title">上传已有 PDF、DOCX 或 TXT</Text>
        <Text className="helper">系统只提取草稿，事实仍需你逐条确认。</Text>
      </View>
      <View className="stage">
        <Text className="stage__number">3.0 · 对齐岗位</Text>
        <Text className="stage__title">识别要求并审阅优化建议</Text>
      </View>
      <View className="fixed-actions">
        <PrimaryAction label="创建基础简历" onClick={() => Taro.navigateTo({ url: "/subpackages/create/index" })} />
      </View>
    </View>
  );
}
