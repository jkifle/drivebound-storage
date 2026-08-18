"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

export type PasskeyViewModel = {
  id: string;
  label: string;
  createdAt: string;
  lastUsedAt: string | null;
};

type PasskeyManagerProps = {
  credentials: PasskeyViewModel[];
  supported: boolean;
  platformAvailable: boolean;
  busyAction: string | null;
  onEnroll: () => void;
  onRename: (credentialId: string, label: string) => void;
  onRemove: (credentialId: string) => void;
};

export function PasskeyManager({ credentials, supported, platformAvailable, busyAction, onEnroll, onRename, onRemove }: PasskeyManagerProps) {
  const [renaming, setRenaming] = useState<string | null>(null);
  const [label, setLabel] = useState("");
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null);
  const renameInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!renaming) return;
    const frame = window.requestAnimationFrame(() => renameInputRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [renaming]);

  const beginRename = (credential: PasskeyViewModel) => {
    setConfirmRemove(null);
    setRenaming(credential.id);
    setLabel(credential.label);
  };

  const rename = (event: FormEvent, credentialId: string) => {
    event.preventDefault();
    if (!label.trim()) return;
    onRename(credentialId, label.trim());
    setRenaming(null);
  };

  const remove = (credentialId: string) => {
    if (confirmRemove !== credentialId) {
      setRenaming(null);
      setConfirmRemove(credentialId);
      return;
    }
    onRemove(credentialId);
    setConfirmRemove(null);
  };

  return <div className="settings-card passkey-settings">
    <h3>Passkeys</h3>
    <p>Use your device unlock, fingerprint, face, or security key instead of typing your password. Drivebound stores only the public credential.</p>
    {!supported && <p className="security-note" role="status">Passkeys require a supported browser and a secure Drivebound address. Password, Google, and authenticator-code sign-in remain available.</p>}
    {supported && <><p className="security-note">{platformAvailable ? "This device can create a built-in passkey." : "You can use a synced passkey or external security key if your browser offers one."}</p><button type="button" className="secondary-button" onClick={onEnroll} disabled={busyAction != null}>{busyAction === "passkey-enroll" ? "Waiting for your device..." : "Add a passkey"}</button></>}
    <div className="passkey-list" aria-label="Registered passkeys">
      {credentials.map((credential) => <article className="passkey-row" key={credential.id}>
        <div><strong>{credential.label}</strong><small>Added {new Date(credential.createdAt).toLocaleString()} - {credential.lastUsedAt ? `Last used ${new Date(credential.lastUsedAt).toLocaleString()}` : "Not used yet"}</small></div>
        <div className="passkey-actions"><button type="button" aria-expanded={renaming === credential.id} aria-controls={`passkey-rename-${credential.id}`} onClick={() => beginRename(credential)} disabled={busyAction != null}>Rename</button><button type="button" className={confirmRemove === credential.id ? "danger confirm-danger" : "danger"} aria-pressed={confirmRemove === credential.id} onClick={() => remove(credential.id)} disabled={busyAction != null}>{busyAction === `passkey-remove-${credential.id}` ? "Removing..." : confirmRemove === credential.id ? "Confirm removal" : "Remove"}</button></div>
        {renaming === credential.id && <form id={`passkey-rename-${credential.id}`} className="passkey-rename" onSubmit={(event) => rename(event, credential.id)}><label className="sr-only" htmlFor={`passkey-name-${credential.id}`}>Passkey name</label><input ref={renameInputRef} id={`passkey-name-${credential.id}`} value={label} onChange={(event) => setLabel(event.target.value)} maxLength={100} /><button disabled={busyAction != null || !label.trim()}>{busyAction === `passkey-rename-${credential.id}` ? "Saving..." : "Save name"}</button><button type="button" onClick={() => setRenaming(null)} disabled={busyAction != null}>Cancel</button></form>}
      </article>)}
      {credentials.length === 0 && <p className="security-note">No passkeys are registered yet.</p>}
    </div>
  </div>;
}
