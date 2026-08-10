import { useState } from "react";

import { ApiError, selectLocalPath, type PathPickerPurpose } from "../api";

export type PathPickerCopy = {
  selectLabel: string;
  selectingLabel: string;
  busyMessage: string;
  unavailableMessage: string;
  failedMessage: string;
};

export type PathPickerProps = {
  id: string;
  value: string;
  purpose: PathPickerPurpose;
  disabled: boolean;
  placeholder: string;
  copy: PathPickerCopy;
  onChange: (value: string) => void;
};

export function PathPicker({ id, value, purpose, disabled, placeholder, copy, onChange }: PathPickerProps) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectPath = async () => {
    if (disabled || pending) return;
    setPending(true);
    setError(null);
    try {
      const result = await selectLocalPath(purpose, value || null);
      if (!result.cancelled && result.path) onChange(result.path);
    } catch (cause) {
      setError(
        cause instanceof ApiError && cause.status === 409 ? copy.busyMessage
          : cause instanceof ApiError && cause.status === 503 ? copy.unavailableMessage
            : copy.failedMessage,
      );
    } finally {
      setPending(false);
    }
  };

  return <div className="path-picker">
    <div className="path-picker-control">
      <input
        id={id}
        type="text"
        disabled={disabled}
        value={value}
        placeholder={placeholder}
        onChange={(event) => {
          setError(null);
          onChange(event.target.value);
        }}
      />
      <button type="button" disabled={disabled || pending} aria-busy={pending} onClick={() => void selectPath()}>
        {pending ? copy.selectingLabel : copy.selectLabel}
      </button>
    </div>
    {error && <small className="path-picker-error" role="alert">{error}</small>}
  </div>;
}
