import type { ButtonHTMLAttributes, ReactNode } from "react";

export type ControlState =
  | "default"
  | "hover"
  | "focus"
  | "active"
  | "disabled"
  | "loading"
  | "error"
  | "success";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  children: ReactNode;
  state?: ControlState;
  variant?: "primary" | "secondary" | "quiet";
}

export function Button({
  children,
  disabled,
  state = "default",
  variant = "primary",
  ...props
}: ButtonProps) {
  const inaccessible = disabled || state === "disabled" || state === "loading";
  return (
    <button
      {...props}
      aria-busy={state === "loading" || undefined}
      aria-disabled={inaccessible || undefined}
      aria-label={props["aria-label"] ?? (typeof children === "string" ? children : undefined)}
      className={`button button--${variant} is-${state} ${props.className ?? ""}`}
      disabled={inaccessible}
      data-state={state}
    >
      <span aria-hidden="true" className="button__state">
        {state === "loading" ? "↻" : state === "error" ? "!" : state === "success" ? "✓" : ""}
      </span>
      <span>{children}</span>
    </button>
  );
}
