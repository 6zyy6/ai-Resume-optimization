import { Button, View } from "@tarojs/components";
import type { ButtonProps } from "@tarojs/components/types/Button";

export type ActionState =
  | "default"
  | "hover"
  | "focus"
  | "active"
  | "disabled"
  | "loading"
  | "error"
  | "success";

interface Props extends Omit<ButtonProps, "loading" | "disabled"> {
  disabled?: boolean;
  state?: ActionState;
  label: string;
}

export function PrimaryAction({ state = "default", label, className = "", disabled: disabledProp, ...props }: Props) {
  const busy = state === "loading";
  const disabled = disabledProp || state === "disabled" || busy;
  return (
    <Button
      {...props}
      aria-label={label}
      className={`primary-action primary-action--${state} ${className}`}
      disabled={disabled}
      loading={busy}
    >
      <View>{busy ? "处理中…" : label}</View>
    </Button>
  );
}
