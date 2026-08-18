"use client";

import Link from "../safe-link";
import { FormEvent, useEffect, useRef, useState } from "react";
import { API_URL, apiRequest } from "../api-client";
import { apiProblemMessage } from "../api-problem";
import { beginPasskeyLogin, finishPasskeyLogin } from "../passkey-api";
import { describePasskeyError, getPasskey, passkeysSupported, platformAuthenticatorAvailable } from "../webauthn";

type Mode = "login" | "register" | "forgot";
type ProviderState = "loading" | "enabled" | "disabled";
type MessageTone = "info" | "success" | "error";
type BusyMethod = "form" | "passkey" | null;

export function AuthPage({ initialMode, initialError }: { initialMode: Mode; initialError: string | null }) {
  const [mode, setMode] = useState<Mode>(initialMode);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [otp, setOtp] = useState("");
  const [needsMfa, setNeedsMfa] = useState(false);
  const [busyMethod, setBusyMethod] = useState<BusyMethod>(null);
  const [message, setMessage] = useState<string | null>(initialError);
  const [messageTone, setMessageTone] = useState<MessageTone>(initialError ? "error" : "info");
  const [actionUrl, setActionUrl] = useState<string | null>(null);
  const [googleState, setGoogleState] = useState<ProviderState>("loading");
  const [googleUrl, setGoogleUrl] = useState(`${API_URL}/api/v1/auth/google/start`);
  const [passkeySupport, setPasskeySupport] = useState<"checking" | "supported" | "unsupported">("checking");
  const [platformPasskey, setPlatformPasskey] = useState(false);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const busy = busyMethod !== null;

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

  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (!passkeysSupported()) { setPasskeySupport("unsupported"); return; }
      setPasskeySupport("supported");
      void platformAuthenticatorAvailable().then(setPlatformPasskey);
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    titleRef.current?.focus();
  }, [mode]);

  const switchMode = (next: Mode) => {
    setMode(next);
    setMessage(null);
    setMessageTone("info");
    setActionUrl(null);
    setNeedsMfa(false);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusyMethod("form");
    setMessage(null);
    setMessageTone("info");
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
          setMessageTone("info");
          return;
        }
        throw new Error(apiProblemMessage(result, "The request could not be completed"));
      }
      if (mode === "login") { window.location.assign("/library"); return; }
      setMessage(result.message);
      setMessageTone("success");
      setActionUrl(result.development_verification_url ?? result.development_url ?? null);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "The request could not be completed");
      setMessageTone("error");
    } finally {
      setBusyMethod(null);
    }
  };

  const signInWithPasskey = async () => {
    setBusyMethod("passkey");
    setMessage("Follow your browser's prompt to use a passkey.");
    setMessageTone("info");
    setActionUrl(null);
    try {
      const options = await beginPasskeyLogin(email.trim() || undefined);
      const credential = await getPasskey(options.public_key);
      await finishPasskeyLogin(options.challenge_id, credential);
      window.location.assign("/library");
    } catch (reason) {
      setMessage(describePasskeyError(reason));
      setMessageTone("error");
    } finally {
      setBusyMethod(null);
    }
  };

  const title = mode === "login" ? "Sign in" : mode === "register" ? "Create account" : "Reset password";
  const googleLabel = googleState === "loading" ? "Checking Google sign-in..." : googleState === "disabled" ? "Google sign-in unavailable" : "Continue with Google";
  return <main className="auth-shell" id="main-content">
    <section className="auth-story" data-reveal="left"><Link className="brand" href="/"><span className="brand-mark" aria-hidden="true">D</span>Drivebound</Link><div className="depth-orbit" aria-hidden="true"><span><i>DB</i></span></div><div className="auth-copy"><span className="eyebrow">Your storage. Your cloud.</span><h1>One secure doorway to every drive.</h1><p>Create a private account, verify ownership of your email, and keep control of every active session.</p></div></section>
    <section className="auth-form-wrap" data-reveal="right"><form className="auth-card" onSubmit={submit} aria-busy={busy}>
      <span className="eyebrow">{mode === "login" ? "Welcome back" : mode === "register" ? "Create your private cloud" : "Account recovery"}</span><h2 ref={titleRef} tabIndex={-1}>{title}</h2><p>{mode === "forgot" ? "We will send a single-use recovery link if the account exists." : "Your credentials stay separate from every other Drivebound library."}</p>
      {mode === "login" && <><button type="button" className="passkey-button" disabled={busy || passkeySupport !== "supported"} onClick={() => void signInWithPasskey()} aria-describedby="passkey-login-note"><span aria-hidden="true">PK</span>{busyMethod === "passkey" ? "Waiting for passkey..." : passkeySupport === "checking" ? "Checking passkey support..." : passkeySupport === "unsupported" ? "Passkeys unavailable here" : "Sign in with a passkey"}</button><small className="provider-note" id="passkey-login-note">{passkeySupport === "unsupported" ? "Use password or Google sign-in. Passkeys require a supported browser and a secure Drivebound address." : platformPasskey ? "Uses this device's screen lock, fingerprint, or face verification." : "Your browser may offer a synced passkey or external security key."}</small></>}
      {mode !== "forgot" && <><button type="button" className="google-button" disabled={busy || googleState !== "enabled"} onClick={() => window.location.assign(googleUrl)} aria-describedby={googleState === "disabled" ? "google-provider-note" : undefined}><span aria-hidden="true">G</span>{googleLabel}</button>{googleState === "disabled" && <small className="provider-note" id="google-provider-note">Add Google OAuth credentials to the server to enable this option.</small>}<div className="auth-divider"><span>or use email</span></div></>}
      {mode === "register" && <label>Display name<input required value={name} onChange={(event) => setName(event.target.value)} autoComplete="name" /></label>}
      <label>Email address<input required type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" /></label>
      {mode !== "forgot" && <label>Password<input required type="password" minLength={mode === "register" ? 12 : 1} value={password} onChange={(event) => setPassword(event.target.value)} autoComplete={mode === "register" ? "new-password" : "current-password"} /></label>}
      {needsMfa && <label>Two-factor or recovery code<input required value={otp} onChange={(event) => setOtp(event.target.value)} autoComplete="one-time-code" /></label>}
      {message && <p className={messageTone === "error" ? "form-error" : "form-success"} role={messageTone === "error" ? "alert" : "status"}>{message}</p>}
      {actionUrl && <a className="development-link" href={actionUrl}>Open development email link</a>}
      <button className="primary-button" disabled={busy}>{busyMethod === "form" ? "One moment..." : mode === "login" ? "Open Drivebound" : mode === "register" ? "Create account" : "Send reset link"}</button>
      {mode === "login" && <button type="button" className="text-button" onClick={() => switchMode("forgot")}>Forgot your password?</button>}
      <button type="button" className="text-button" onClick={() => switchMode(mode === "register" ? "login" : mode === "forgot" ? "login" : "register")}>{mode === "register" ? "Already have an account? Sign in" : mode === "forgot" ? "Back to sign in" : "New to Drivebound? Create an account"}</button>
    </form></section>
  </main>;
}
