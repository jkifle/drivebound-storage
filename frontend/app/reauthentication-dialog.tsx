"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

export type ReauthenticationCredentials = { password: string; code?: string };
export type ReauthenticationMethod = "password" | "passkey" | "google" | null;

type ReauthenticationDialogProps = {
  open: boolean;
  busyMethod: ReauthenticationMethod;
  error: string | null;
  twoFactorEnabled: boolean;
  passwordAvailable: boolean;
  passkeyAvailable: boolean;
  googleAvailable: boolean;
  onCancel: () => void;
  onPassword: (credentials: ReauthenticationCredentials) => void;
  onPasskey: () => void;
  onGoogle: () => void;
};

const focusableSelector = "button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex='-1'])";

export function ReauthenticationDialog({
  open,
  busyMethod,
  error,
  twoFactorEnabled,
  passwordAvailable,
  passkeyAvailable,
  googleAvailable,
  onCancel,
  onPassword,
  onPasskey,
  onGoogle,
}: ReauthenticationDialogProps) {
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const dialogRef = useRef<HTMLDivElement>(null);
  const busy = busyMethod !== null;
  const busyRef = useRef(busy);

  useEffect(() => { busyRef.current = busy; }, [busy]);

  useEffect(() => {
    if (!open) {
      const resetTimer = window.setTimeout(() => { setPassword(""); setCode(""); }, 0);
      return () => window.clearTimeout(resetTimer);
    }
    const dialog = dialogRef.current;
    if (!dialog) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const frame = window.requestAnimationFrame(() => dialog.querySelector<HTMLElement>(focusableSelector)?.focus());
    const handleKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape" && !busyRef.current) { event.preventDefault(); onCancel(); return; }
      if (event.key !== "Tab") return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(focusableSelector)).filter((element) => element.getClientRects().length > 0);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    dialog.addEventListener("keydown", handleKey);
    return () => {
      window.cancelAnimationFrame(frame);
      dialog.removeEventListener("keydown", handleKey);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, [onCancel, open]);

  if (!open) return null;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    onPassword({ password, code: code.trim() || undefined });
  };

  const alternateMethodAvailable = passkeyAvailable || googleAvailable;
  const busyStatus = busyMethod === "passkey" ? "Waiting for your browser's passkey prompt." : busyMethod === "google" ? "Opening Google verification." : busyMethod === "password" ? "Verifying your password." : "";

  return <div className="reauth-scrim" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="reauth-title" aria-describedby="reauth-description" aria-busy={busy} tabIndex={-1}>
    <section className="reauth-card">
      <div className="reauth-heading"><div><span className="eyebrow">Confirm it is you</span><h2 id="reauth-title">Verify this sensitive change</h2></div><button className="icon-button" type="button" onClick={onCancel} disabled={busy} aria-label="Cancel verification">X</button></div>
      <p id="reauth-description">Your session is still signed in, but Drivebound requires a fresh verification before continuing this security-sensitive action.</p>
      <p className="sr-only" role="status" aria-live="polite">{busyStatus}</p>
      {passkeyAvailable && <button className="passkey-button" type="button" onClick={onPasskey} disabled={busy}><span aria-hidden="true">PK</span>{busyMethod === "passkey" ? "Waiting for passkey..." : "Verify with a passkey"}</button>}
      {googleAvailable && <button className="google-button" type="button" onClick={onGoogle} disabled={busy}><span aria-hidden="true">G</span>{busyMethod === "google" ? "Opening Google..." : "Verify again with Google"}</button>}
      {googleAvailable && <small className="provider-note">Google freshly verifies this session. You will return to Security and can retry this change.</small>}
      {alternateMethodAvailable && passwordAvailable && <div className="auth-divider" aria-hidden="true"><span>or verify with password</span></div>}
      {passwordAvailable && <form onSubmit={submit}>
        <label>Password<input type="password" required value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></label>
        {twoFactorEnabled && <label>Authenticator or recovery code<input required value={code} onChange={(event) => setCode(event.target.value)} autoComplete="one-time-code" /></label>}
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="button-row"><button className="secondary-button" type="button" onClick={onCancel} disabled={busy}>Cancel</button><button className="primary-button" disabled={busy || !password || (twoFactorEnabled && !code.trim())}>{busy ? "Verifying..." : "Verify and continue"}</button></div>
      </form>}
      {!passwordAvailable && !alternateMethodAvailable && <p className="form-error" role="alert">No reauthentication method is available. Contact your Drivebound administrator before retrying this change.</p>}
      {!passwordAvailable && error && <p className="form-error" role="alert">{error}</p>}
      {!passwordAvailable && <button className="secondary-button" type="button" onClick={onCancel} disabled={busy}>Cancel</button>}
    </section>
  </div>;
}
