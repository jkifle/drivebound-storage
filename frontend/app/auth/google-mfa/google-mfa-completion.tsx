"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { API_URL } from "../../api-client";
import { completeGoogleMfa, GoogleMfaCompletionError, type GoogleMfaMode } from "../../google-mfa-api";
import Link from "../../safe-link";

const transactionErrors: Record<string, string> = {
  google_mfa_transaction_invalid: "This Google verification expired or was already used. Start again to receive a new verification prompt.",
  google_mfa_code_invalid: "That authenticator or recovery code was not accepted. This verification attempt is now closed; start again to continue safely.",
};

export function GoogleMfaCompletion({ mode }: { mode: GoogleMfaMode | null }) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(mode ? null : "This Google verification link is invalid.");
  const [transactionClosed, setTransactionClosed] = useState(!mode);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const codeRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => (mode ? codeRef.current : titleRef.current)?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [mode]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!mode || !code.trim()) return;
    setBusy(true);
    setMessage(null);
    try {
      await completeGoogleMfa(mode, code.trim());
      window.location.replace(mode === "login" ? "/library" : "/profile?tab=security&reauthenticated=google");
    } catch (reason) {
      if (reason instanceof GoogleMfaCompletionError) {
        const closed = reason.code === "google_mfa_transaction_invalid" || reason.code === "google_mfa_code_invalid";
        setTransactionClosed(closed);
        setMessage(reason.code ? transactionErrors[reason.code] ?? reason.message : reason.message);
      } else {
        setMessage("Drivebound could not reach the verification service. Check the connection and try again.");
      }
      setCode("");
      window.requestAnimationFrame(() => titleRef.current?.focus());
    } finally {
      setBusy(false);
    }
  };

  const restartUrl = mode === "reauthenticate" ? `${API_URL}/api/v1/auth/reauthenticate/google/start` : "/auth";
  return <main className="centered-page" id="main-content">
    <section className="token-card" aria-busy={busy}>
      <Link className="brand" href="/"><span className="brand-mark" aria-hidden="true">D</span>Drivebound</Link>
      <div className="step-icon" aria-hidden="true">2</div>
      <span className="eyebrow">Local account protection</span>
      <h1 ref={titleRef} tabIndex={-1}>{mode ? "Finish signing in securely." : "Verification unavailable."}</h1>
      <p>{mode ? "Google verified your identity. Enter the code from your Drivebound authenticator app, or an unused recovery code, within five minutes." : "Return to sign in and start Google verification again."}</p>
      {!transactionClosed && <form onSubmit={(event) => void submit(event)}>
        <label>Authenticator or recovery code<input ref={codeRef} required value={code} onChange={(event) => setCode(event.target.value)} autoComplete="one-time-code" /></label>
        <button className="primary-button" disabled={busy || !code.trim()}>{busy ? "Verifying..." : "Verify and continue"}</button>
      </form>}
      {message && <p className="form-error" role="alert">{message}</p>}
      {transactionClosed && (mode === "reauthenticate" ? <a className="primary-button" href={restartUrl}>Restart Google verification</a> : <Link className="primary-button" href={restartUrl}>Return to sign in</Link>)}
      {!transactionClosed && <small className="provider-note">The temporary Google verification is HttpOnly and is never exposed to this page.</small>}
    </section>
  </main>;
}
