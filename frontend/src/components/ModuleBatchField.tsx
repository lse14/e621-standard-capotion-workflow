import { FormField, type FieldGuidanceCopy } from "./FormField";

export type ModuleBatchFieldProps = {
  id: string;
  label: string;
  value: number;
  defaultValue: number;
  recommended?: number;
  recommendationReason?: string;
  minimum: number;
  maximum: number;
  disabled: boolean;
  t: (key: string, values?: Record<string, string | number>) => string;
  guidanceCopy: FieldGuidanceCopy;
  onChange: (value: number) => void;
};

export function ModuleBatchField({
  id, label, value, defaultValue, recommended, recommendationReason, minimum, maximum, disabled, t, guidanceCopy, onChange,
}: ModuleBatchFieldProps) {
  const risky = typeof recommended === "number" && value > recommended;
  const guidance = {
    description: t("fieldHelp_moduleBatchSize"),
    defaultValue: String(defaultValue),
    recommendation: typeof recommended === "number" ? String(recommended) : null,
    range: `${minimum}-${maximum}`,
  };
  return <div className="module-batch-field">
    <FormField id={id} label={label} copy={guidanceCopy} guidance={guidance}>
      <input
        id={id}
        disabled={disabled}
        type="number"
        min={minimum}
        max={maximum}
        step="1"
        value={value}
        onChange={(event) => {
          const next = Number(event.target.value);
          if (Number.isInteger(next)) onChange(Math.min(maximum, Math.max(minimum, next)));
        }}
      />
    </FormField>
    {risky && <small className="resource-warning module-batch-risk" role="alert" data-batch-risk={id}>
      {t("moduleBatchRisk", { value, recommended })}{recommendationReason ? ` ${recommendationReason}` : ""}
    </small>}
  </div>;
}
