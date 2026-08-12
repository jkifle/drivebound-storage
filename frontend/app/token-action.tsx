"use client";

import Link from "./safe-link";
import { FormEvent, useEffect, useState } from "react";
import { apiRequest } from "./api-client";

export function TokenAction({ kind, token }: { kind: "verify" | "reset"; token: string }) {
  const [password, setPassword] = useState(""); const [confirm, setConfirm] = useState(""); const [message, setMessage] = useState<string | null>(null); const [done, setDone] = useState(false); const [busy, setBusy] = useState(kind === "verify");
  const perform = async (event?: FormEvent) => {
    event?.preventDefault(); if (!token) { setMessage("This link is missing its token."); setBusy(false); return; }
    if (kind === "reset" && password !== confirm) { setMessage("Passwords do not match."); return; }
    setBusy(true); const response = await apiRequest(`/api/v1/auth/${kind === "verify" ? "verify-email" : "reset-password"}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(kind === "verify" ? { token } : { token, new_password: password }) }, false); const result = await response.json(); setMessage(result.message ?? result.detail); setDone(response.ok); setBusy(false);
  };
  useEffect(() => { if (kind === "verify") { const timer = window.setTimeout(() => void perform(), 0); return () => window.clearTimeout(timer); } }, []); // eslint-disable-line react-hooks/exhaustive-deps
  return <main className="centered-page"><section className="token-card"><Link className="brand" href="/"><span className="brand-mark">D</span>Drivebound</Link><div className={`step-icon ${done ? "success" : ""}`}>{done ? "âœ“" : kind === "verify" ? "@" : "â†‘"}</div><span className="eyebrow">{kind === "verify" ? "Email verification" : "Password recovery"}</span><h1>{done ? "You are all set." : kind === "verify" ? "Verifying your emailâ€¦" : "Choose a new password."}</h1>{kind === "reset" && !done && <form onSubmit={(e) => void perform(e)}><label>New password<input type="password" minLength={12} required value={password} onChange={(e) => setPassword(e.target.value)} /></label><label>Confirm password<input type="password" minLength={12} required value={confirm} onChange={(e) => setConfirm(e.target.value)} /></label><button className="primary-button" disabled={busy}>Reset password</button></form>}{message && <p className={done ? "form-success" : "form-error"}>{message}</p>}{done && <Link className="primary-button" href="/auth">Continue to sign in</Link>}</section></main>;
}
