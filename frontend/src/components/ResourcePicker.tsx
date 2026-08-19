import type { UiLanguage } from "../i18n";
import { FormField, type FieldGuidance, type FieldGuidanceCopy } from "./FormField";

export type ResourceSelectableResource = {
  available: boolean;
  compatibility: { status: string };
};

type ResourcePickerResource = ResourceSelectableResource & {
  resourceId: string;
  resourceVersion: string;
  displayName: Record<UiLanguage, string>;
  description: Record<UiLanguage, string>;
  distribution: { mode: "bundled" | "local-only"; sourceUrl?: string; licenseUrl?: string };
  adjustableCategories: string[];
  excludedCategories: string[];
  default?: boolean;
  officialModelId?: string;
};

export const resourceSelectable = (resource: ResourceSelectableResource | undefined): boolean => Boolean(
  resource?.available && !["incompatible", "unavailable"].includes(resource.compatibility.status),
);

const manualTaggerInstall = {
  "caption-danbooru-cl-tagger-v2-00": {
    directory: "resource-library/tagging-models/caption-danbooru-cl-tagger-v2-00/",
    files: ["model.onnx", "model.onnx.data", "model_metadata.json", "model_vocabulary.json"],
  },
  "caption-danbooru-wd-eva02-large-v3": {
    directory: "resource-library/tagging-models/caption-danbooru-wd-eva02-large-v3/",
    files: ["model.onnx", "selected_tags.csv"],
  },
} as const;

export type ResourcePickerProps = {
  id?: string;
  label: string;
  language: UiLanguage;
  selectedId: string;
  resources: ResourcePickerResource[];
  loading: boolean;
  error: string | null;
  invalidCount: number;
  selectionDisabled: boolean;
  refreshDisabled: boolean;
  note: string;
  guidance?: FieldGuidance;
  guidanceCopy?: FieldGuidanceCopy;
  copy: {
    loading: string; unavailable: string; refresh: string; retry: string; details: string; version: string;
    id: string; status: string; distribution: string; ready: string; notInstalled: string; incompatible: string;
    defaultSuffix: string; localOnly: string; bundled: string; source: string; license: string;
    adjustable: string; excluded: string; manualInstall: string; downloadUrl: string; installDirectory: string;
    requiredFiles: string; installHint: string; invalid: (count: number) => string;
  };
  onChange: (resourceId: string) => void;
  onRefresh: () => void;
};

export function ResourcePicker({
  id, label, language, selectedId, resources, loading, error, invalidCount, selectionDisabled, refreshDisabled,
  note, guidance, guidanceCopy, copy, onChange, onRefresh,
}: ResourcePickerProps) {
  const selected = resources.find((item) => item.resourceId === selectedId);
  const resourceName = (item: ResourcePickerResource) => item.displayName[language] || item.officialModelId || item.resourceId;
  const recommended = resources.find((item) => item.default === true);
  const effectiveGuidance = guidance ? { ...guidance, recommendation: recommended ? resourceName(recommended) : null } : undefined;
  const controlId = id ?? `resource-${selectedId || "picker"}`;
  const install = selected && !selected.available && selected.distribution.mode === "local-only"
    ? manualTaggerInstall[selected.resourceId as keyof typeof manualTaggerInstall]
    : undefined;
  const selectedStatus = !selected?.available
    ? copy.notInstalled
    : selected.compatibility.status === "incompatible" || selected.compatibility.status === "unavailable"
      ? copy.incompatible
      : copy.ready;

  const refreshLabel = error ? copy.retry : copy.refresh;

  const selector = <div className="resource-control">
        <select id={controlId} disabled={selectionDisabled || loading || resources.length === 0} value={selectedId} onChange={(event) => onChange(event.target.value)}>
          {loading && <option value={selectedId}>{copy.loading}</option>}
          {!loading && resources.length === 0 && <option value={selectedId}>{copy.unavailable}</option>}
          {!loading && selectedId && !selected && <option value={selectedId}>{selectedId}</option>}
          {!loading && resources.map((item) => <option key={item.resourceId} value={item.resourceId} disabled={!resourceSelectable(item)}>
            {resourceName(item)}{item.default ? copy.defaultSuffix : ""}{!item.available ? ` - ${copy.notInstalled}` : ""}
          </option>)}
        </select>
        <button className="icon-button secondary" type="button" disabled={refreshDisabled || loading} onClick={onRefresh} title={refreshLabel} aria-label={refreshLabel}>
          <span aria-hidden="true">&#8635;</span>
        </button>
      </div>;

  return <section className="resource-picker" aria-busy={loading}>
    {effectiveGuidance && guidanceCopy
      ? <FormField id={controlId} label={label} guidance={effectiveGuidance} copy={guidanceCopy}>{selector}</FormField>
      : <label>{label}{selector}</label>}
    {loading && <small className="resource-loading" role="status">{copy.loading}</small>}
    {error && <small className="resource-error" role="alert">{error}</small>}
    {invalidCount > 0 && <small className="resource-warning">{copy.invalid(invalidCount)}</small>}
    {selected && <details className="resource-details">
      <summary>{copy.details}</summary>
      <p>{selected.description[language] || selected.officialModelId || selected.resourceId}</p>
      <dl>
        <div><dt>{copy.version}</dt><dd>{selected.resourceVersion}</dd></div>
        <div><dt>{copy.id}</dt><dd><code>{selected.resourceId}</code></dd></div>
        <div><dt>{copy.status}</dt><dd>{selectedStatus}</dd></div>
        <div><dt>{copy.distribution}</dt><dd>{selected.distribution.mode === "local-only" ? copy.localOnly : copy.bundled}</dd></div>
        {selected.adjustableCategories.length > 0 && <div><dt>{copy.adjustable}</dt><dd>{selected.adjustableCategories.join(" / ")}</dd></div>}
        {selected.excludedCategories.length > 0 && <div><dt>{copy.excluded}</dt><dd>{selected.excludedCategories.join(" / ")}</dd></div>}
        {selected.distribution.sourceUrl && !install && <div><dt>{copy.source}</dt><dd><a href={selected.distribution.sourceUrl} target="_blank" rel="noreferrer">{selected.distribution.sourceUrl}</a></dd></div>}
        {selected.distribution.licenseUrl && <div><dt>{copy.license}</dt><dd><a href={selected.distribution.licenseUrl} target="_blank" rel="noreferrer">{copy.license}</a></dd></div>}
      </dl>
      {install && <section className="manual-install" aria-label={copy.manualInstall}>
        <strong>{copy.manualInstall}</strong>
        <dl>
          {selected.distribution.sourceUrl && <div><dt>{copy.downloadUrl}</dt><dd><a href={selected.distribution.sourceUrl} target="_blank" rel="noreferrer">{selected.distribution.sourceUrl}</a></dd></div>}
          <div><dt>{copy.installDirectory}</dt><dd><code>{install.directory}</code></dd></div>
          <div><dt>{copy.requiredFiles}</dt><dd><code>{install.files.join(", ")}</code></dd></div>
        </dl>
        <small>{copy.installHint}</small>
      </section>}
      <small>{note}</small>
    </details>}
  </section>;
}
