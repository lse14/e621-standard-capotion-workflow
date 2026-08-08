import { useEffect, useId, useRef, useState, type ReactNode } from "react";

export type FieldGuidance = {
  description: string;
  defaultValue?: string | null;
  recommendation?: string | null;
  range?: string | null;
};

export type FieldGuidanceCopy = {
  informationFor: (label: string) => string;
  defaultValue: string;
  recommendation: string;
  range: string;
};

export type FormFieldProps = {
  id?: string;
  label: string;
  guidance?: FieldGuidance;
  copy: FieldGuidanceCopy;
  wide?: boolean;
  className?: string;
  children: ReactNode;
};

export type ToggleFieldProps = {
  id: string;
  label: string;
  checked: boolean;
  disabled: boolean;
  guidance: FieldGuidance;
  copy: FieldGuidanceCopy;
  onChange: (checked: boolean) => void;
  wide?: boolean;
};

type Translate = (key: string, values?: Record<string, string | number>) => string;

export function makeFieldGuidanceCopy(t: Translate): FieldGuidanceCopy {
  return {
    informationFor: (label) => t("fieldInformation", { label }),
    defaultValue: t("fieldDefaultValue"),
    recommendation: t("fieldRecommendedValue"),
    range: t("fieldRange"),
  };
}

export function FieldHelp({ label, guidance, copy }: {
  label: string;
  guidance: FieldGuidance;
  copy: FieldGuidanceCopy;
}) {
  const tooltipId = useId();
  const rootRef = useRef<HTMLSpanElement>(null);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [pinned, setPinned] = useState(false);
  const open = hovered || focused || pinned;

  useEffect(() => {
    if (!pinned) return undefined;
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setPinned(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [pinned]);

  return <span
    ref={rootRef}
    className="field-help-control"
    onMouseEnter={() => setHovered(true)}
    onMouseLeave={() => setHovered(false)}
  >
    <button
      className="field-info-button"
      type="button"
      aria-label={copy.informationFor(label)}
      aria-expanded={open}
      aria-describedby={open ? tooltipId : undefined}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      onClick={() => setPinned((value) => !value)}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          setPinned(false);
          event.currentTarget.blur();
        }
      }}
    ><span aria-hidden="true">i</span></button>
    {open && <span className="field-tooltip" id={tooltipId} role="tooltip">
      <span>{guidance.description}</span>
      {guidance.defaultValue != null && <span><strong>{copy.defaultValue}:</strong> {guidance.defaultValue}</span>}
      {guidance.recommendation != null && <span><strong>{copy.recommendation}:</strong> {guidance.recommendation}</span>}
      {guidance.range != null && <span><strong>{copy.range}:</strong> {guidance.range}</span>}
    </span>}
  </span>;
}

export function FormField({ id, label, guidance, copy, wide = false, className = "", children }: FormFieldProps) {
  return <div data-setting-field className={`form-field${wide ? " wide" : ""}${className ? ` ${className}` : ""}`}>
    <div className="field-label-row">
      {id ? <label htmlFor={id}>{label}</label> : <span className="field-label">{label}</span>}
      {guidance && <FieldHelp label={label} guidance={guidance} copy={copy} />}
    </div>
    <div className="field-control">{children}</div>
  </div>;
}

export function ToggleField({ id, label, checked, disabled, guidance, copy, onChange, wide = false }: ToggleFieldProps) {
  return <div data-setting-field className={`form-field toggle-field${wide ? " wide" : ""}`}>
    <div className="field-toggle-row">
      <label className="checkbox" htmlFor={id}>
        <input id={id} disabled={disabled} type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
        <span>{label}</span>
      </label>
      <FieldHelp label={label} guidance={guidance} copy={copy} />
    </div>
  </div>;
}
