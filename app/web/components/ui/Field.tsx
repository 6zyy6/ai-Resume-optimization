import type { InputHTMLAttributes, TextareaHTMLAttributes } from "react";
import type { ControlState } from "./Button";

type FieldProps = {
  error?: string;
  helper?: string;
  label: string;
  multiline?: boolean;
  name: string;
  state?: ControlState;
} & (InputHTMLAttributes<HTMLInputElement> | TextareaHTMLAttributes<HTMLTextAreaElement>);

export function Field({
  error,
  helper,
  label,
  multiline,
  name,
  state = "default",
  ...props
}: FieldProps) {
  const descriptionId = `${name}-description`;
  const common = {
    ...props,
    "aria-describedby": descriptionId,
    "aria-invalid": Boolean(error),
    className: `field__control is-${error ? "error" : state} ${props.className ?? ""}`,
    "data-state": error ? "error" : state,
    disabled: props.disabled || state === "disabled",
    id: name,
    name,
  };
  return (
    <label className="field" htmlFor={name}>
      <span className="field__label">{label}</span>
      {multiline ? <textarea {...common as TextareaHTMLAttributes<HTMLTextAreaElement>} /> : <input {...common as InputHTMLAttributes<HTMLInputElement>} />}
      <span className={`field__message ${error ? "field__message--error" : ""}`} id={descriptionId}>
        {error ? `! ${error}` : helper ?? " "}
      </span>
    </label>
  );
}
