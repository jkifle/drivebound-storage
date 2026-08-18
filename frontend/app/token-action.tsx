"use client";

import Link from "./safe-link";
import { FormEvent, useEffect, useRef, useState } from "react";
import { apiRequest } from "./api-client";
import { apiProblemMessage } from "./api-problem";

export function TokenAction({ kind, legacyToken }: { kind: "verify" | "reset"; legacyToken: string }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const [busy, setBusy] = useState(kind === "verify");
  const tokenRef = useRef("");

  const perform = async (event?: FormEvent) => {
    event?.preventDefault();
    setMessage(null);
    const token = tokenRef.current;
    if (!token) { setAttempted(true); setMessage("This link is missing or has already used its token. Request a new link."); setBusy(false); return; }
    if (kind === "reset" && password !== confirm) { setMessage("Passwords do not match."); return; }
    setAttempted(true);
    setBusy(true);
    try {
      const response = await apiRequest(`/api/v1/auth/${kind === "verify" ? "verify-email" : "reset-password"}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(kind === "verify" ? { token } : { token, new_password: password }),
      }, false);
      const result = await response.json().catch(() => ({}));
      setMessage(apiProblemMessage(result, response.ok ? "The change is complete." : "This link could not be used."));
      setDone(response.ok);
    } catch {
      setDone(false);
      setMessage("Drivebound could not complete this request. Check the connection and try again.");
    } finally {
      tokenRef.current = "";
      setBusy(false);
    }
  };

  useEffect(() => {
    const url = new URL(window.location.href);
    const fragment = url.hash.startsWith("#") ? url.hash.slice(1) : url.hash;
    const fragmentToken = new URLSearchParams(fragment).get("token") ?? "";
    tokenRef.current = fragmentToken || legacyToken;
    window.history.replaceState(window.history.state, "", url.pathname);
    const timer = kind === "verify" ? window.setTimeout(() => void perform(), 0) : null;
    return () => {
      if (timer !== null) window.clearTimeout(timer);
      tokenRef.current = "";
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return <main className="centered-page" id="main-content"><section className="token-card" aria-busy={busy}>
    <Link className="brand" href="/"><span className="brand-mark" aria-hidden="true">D</span>Drivebound</Link>
    <div className={`step-icon ${done ? "success" : ""}`} aria-hidden="true">{done ? "✓" : kind === "verify" ? "@" : "↑"}</div>
    <span className="eyebrow">{kind === "verify" ? "Email verification" : "Password recovery"}</span>
    <h1>{done ? "You are all set." : kind === "verify" ? "Verifying your email…" : "Choose a new password."}</h1>
    {kind === "reset" && !done && !attempted && <form onSubmit={(event) => void perform(event)}><label>New password<input type="password" minLength={12} required value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" /></label><label>Confirm password<input type="password" minLength={12} required value={confirm} onChange={(event) => setConfirm(event.target.value)} autoComplete="new-password" /></label><button className="primary-button" disabled={busy}>{busy ? "Resetting…" : "Reset password"}</button></form>}
    {message && <p className={done ? "form-success" : "form-error"} role={done ? "status" : "alert"}>{message}</p>}
    {!done && attempted && !busy && <Link className="secondary-button" href="/auth">Request a new link</Link>}
    {done && <Link className="primary-button" href="/auth">Continue to sign in</Link>}
  </section></main>;
}
