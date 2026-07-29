import { Text } from "@tarojs/components";

type Tone = "info" | "pending" | "success" | "error";

const statusMap: Record<string, { label: string; tone: Tone }> = {
  queued: { label: "等待处理", tone: "pending" },
  running: { label: "处理中", tone: "info" },
  succeeded: { label: "已完成", tone: "success" },
  failed: { label: "失败", tone: "error" },
  cancelled: { label: "已取消", tone: "error" },
  confirmed: { label: "已确认", tone: "success" },
  unconfirmed: { label: "待确认", tone: "pending" },
  rejected: { label: "已拒绝", tone: "error" },
};

export function StatusTag({ status }: { status: string }) {
  const entry = statusMap[status] ?? { label: "状态更新中", tone: "info" as const };
  return <Text className={`status-tag status-tag--${entry.tone}`}>{entry.label}</Text>;
}
