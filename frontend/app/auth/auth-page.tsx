"use client";

import Link from "../safe-link";
import { FormEvent, useEffect, useState } from "react";
import { API_URL, apiRequest } from "../api-client";

type Mode = "login" | "register" | "forgot";
type ProviderState = "loading" | "enabled" | "disabled";

export function AuthPage({ initialMode, initialError }: { initialMode: Mode; initialError: string | null }) {
  const [mode, setMode] = useState<Mode>(initialMode);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [otp, setOtp] = useState("");
  const [needsMfa, setNeedsMfa] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(initialError);
  const [actionUrl, setActionUrl] = useState<string | null>(null);
  const [googleState, setGoogleState] = useState<ProviderState>("loading");
  const [googleUrl, setGoogleUrl] = useState(`${API_URL}/api/v1/auth/google/start`);

  useEffect(() => {
    apiRequest("/api/v1/auth/providers", undefined, false)
      .then(async (response) => {
        if (!response.ok) throw new Error();
        const providers = await response.json();
        setGoogleUrl(providers.google_start_url);
        setGoogleState(providers.google ? "enabled" : "disabled");
      })
      .catch(() => setGoogleState("disabled"));
  }, []);

  const switchMode = (next: Mode) => {
    setMode(next);
    setMessage(null);
    setActionUrl(null);
    setNeedsMfa(false);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    setActionUrl(null);
    const path = mode === "forgot" ? "/api/v1/auth/forgot-password" : `/api/v1/auth/${mode}`;
    const body = mode === "register" ? { display_name: name, email, password } : mode === "login" ? { email, password, otp: otp || undefined } : { email };
    try {
      const response = await apiRequest(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }, false);
      const result = await response.json();
      if (!response.ok) {
        if (response.headers.get("X-Drivebound-MFA") === "required") {
          setNeedsMfa(true);
          setMessage("Enter the six-digit code from your authenticator, or a recovery code.");
          return;
        }
        throw new Error(result.detail ?? "The request could not be completed");
      }
      if (mode === "login") { window.location.assign("/library"); return; }
      setMessage(result.message);
      setActionUrl(result.development_verification_url ?? result.development_url ?? null);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "The request could not be completed");
    } finally {
      setBusy(false);
    }
  };

  const title = mode === "login" ? "Sign in" : mode === "register" ? "Create account" : "Reset password";
  return <main className="auth-shell">
    <section className="auth-story" data-reveal="left"><Link className="brand" href="/"><span className="brand-mark">D</span>Drivebound</Link><div className="depth-orbit"><span><i>DB</i></span></div><div className="auth-copy"><span className="eyebrow">Your storage. Your cloud.</span><h1>One secure doorway to every drive.</h1><p>Create a private account, verify ownership of your email, and keep control of every active session.</p></div></section>
    <section className="auth-form-wrap" data-reveal="right"><form className="auth-card" onSubmit={submit}>
      <span className="eyebrow">{mode === "login" ? "Welcome back" : mode === "register" ? "Create your private cloud" : "Account recovery"}</span><h2>{title}</h2><p>{mode === "forgot" ? "We will send a single-use recovery link if the account exists." : "Your credentials stay separate from every other Drivebound library."}</p>
      {mode !== "forgot" && <><button type="button" className="google-button" disabled={googleState !== "enabled"} onClick={() => window.location.assign(googleUrl)}><span aria-hidden="true">G</span>{googleState === "loading" ? "Checking Google sign-in…" : "Continue with Google"}</button>{googleState === "disabled" && <small className="provider-note">Google sign-in appears after its OAuth credentials are added to the server.</small>}<div className="auth-divider"><span>or use email</span></div></>}
      {mode === "register" && <label>Display name<input required value={name} onChange={(event) => setName(event.target.value)} autoComplete="name" /></label>}
      <label>Email address<input required type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" /></label>
      {mode !== "forgot" && <label>Password<input required type="password" minLength={mode === "register" ? 12 : 1} value={password} onChange={(event) => setPassword(event.target.value)} autoComplete={mode === "register" ? "new-password" : "current-password"} /></label>}
      {needsMfa && <label>Two-factor or recovery code<input required value={otp} onChange={(event) => setOtp(event.target.value)} inputMode="numeric" autoComplete="one-time-code" /></label>}
      {message && <p className={actionUrl ? "form-success" : "form-error"} role="status">{message}</p>}
      {actionUrl && <a className="development-link" href={actionUrl}>Open development email link</a>}
      <button className="primary-button" disabled={busy}>{busy ? "One moment…" : mode === "login" ? "Open Drivebound" : mode === "register" ? "Create account" : "Send reset link"}</button>
      {mode === "login" && <button type="button" className="text-button" onClick={() => switchMode("forgot")}>Forgot your password?</button>}
      <button type="button" className="text-button" onClick={() => switchMode(mode === "register" ? "login" : mode === "forgot" ? "login" : "register")}>{mode === "register" ? "Already have an account? Sign in" : mode === "forgot" ? "Back to sign in" : "New to Drivebound? Create an account"}</button>
    </form></section>
  </main>;
}
