import { useEffect, useRef, useState, type ReactNode } from "react";
import type { Sendspin } from "./DeviceSettings";

/**
 * "Name this speaker": shown the first time this device starts listening, so owners and admins
 * see a useful name in their speaker list. Confirming saves the name and starts listening inside
 * the same tap (the audio has to be unlocked in a user gesture).
 */
function NameSpeakerDialog({
  defaultName,
  notes,
  onConfirm,
  onCancel,
}: {
  defaultName: string;
  notes: string;
  onConfirm: (name: string) => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState(defaultName);

  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
    const input = d?.querySelector("input");
    input?.focus();
    input?.select();
    return () => d?.close();
  }, []);

  return (
    <dialog
      ref={ref}
      className="dialog"
      aria-labelledby="name-speaker-title"
      onCancel={(e) => {
        e.preventDefault();
        onCancel();
      }}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) onConfirm(name.trim());
        }}
      >
        <h2 className="dialog__title" id="name-speaker-title">
          Name this speaker
        </h2>
        {notes.trim() && <p className="dialog__notes">{notes.trim()}</p>}
        <label className="field">
          <span className="field__label">Speaker name</span>
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={40}
            enterKeyHint="go"
            autoComplete="off"
            required
          />
          <span className="field__hint">Shown to the people who control the party's speakers.</span>
        </label>
        <div className="dialog__actions">
          <button type="button" className="btn btn--ghost" onClick={onCancel}>
            Cancel
          </button>
          <button type="submit" className="btn btn--primary" disabled={!name.trim()}>
            Start listening
          </button>
        </div>
      </form>
    </dialog>
  );
}

/**
 * Listen, asking for a speaker name first if this device has never been named.
 * `listen()` must be called from a user gesture; render `dialog` somewhere on the page.
 */
export function useListenWithNaming(sp: Sendspin, notes: string): { listen: () => void; naming: boolean; dialog: ReactNode } {
  const [naming, setNaming] = useState(false);
  const listen = () => {
    if (sp.status.listening) return;
    if (sp.named || sp.speaker?.locked) sp.start(); // a locked speaker's name is up to its owner
    else setNaming(true);
  };
  const dialog = naming ? (
    <NameSpeakerDialog
      defaultName={sp.deviceName}
      notes={notes}
      onCancel={() => setNaming(false)}
      onConfirm={(name) => {
        setNaming(false);
        sp.startNamed(name);
      }}
    />
  ) : null;
  return { listen, naming, dialog };
}
