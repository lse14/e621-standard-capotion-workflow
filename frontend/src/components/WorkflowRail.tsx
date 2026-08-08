export type WorkflowRailStep = {
  id: string;
  title: string;
  label: string;
  moduleNumber: number | null;
  isSetup: boolean;
};

export type WorkflowRailProps = {
  flow: string;
  visibleStepIndex: number;
  steps: WorkflowRailStep[];
  onSelect: (index: number) => void;
};

export function WorkflowRail({ flow, visibleStepIndex, steps, onSelect }: WorkflowRailProps) {
  return <nav className="workflow-rail" aria-label={flow}>
    <div className="rail-heading"><span>{flow}</span><strong>{visibleStepIndex + 1} / {steps.length}</strong></div>
    {steps.map((step, index) => <button
      key={step.id}
      type="button"
      className={`step-nav ${index === visibleStepIndex ? "active" : ""} ${index < visibleStepIndex ? "visited" : ""}`}
      aria-label={`${step.moduleNumber === null ? "" : `${step.moduleNumber} `}${step.title} ${step.label}`}
      onClick={() => onSelect(index)}
    >
      <span className={`step-index ${step.isSetup ? "setup-index" : ""}`}>{step.isSetup ? "\u2022" : step.moduleNumber}</span>
      <span><strong>{step.title}</strong><small>{step.label}</small></span>
    </button>)}
  </nav>;
}
