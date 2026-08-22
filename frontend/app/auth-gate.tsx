"use client";

import { useEffect, useRef, useState } from "react";
import { apiRequest } from "./api-client";
import { PhotoLibrary } from "./photo-library";

export type DriveboundUser = {
  id: string;
  email: string;
  display_name: string | null;
  onboarding_completed_at: string | null;
  email_verified_at: string | null;
  two_factor_enabled: boolean;
  has_password: boolean;
  reauth_methods: Array<"password" | "google" | "passkey">;
};

function Brand() {
  return <div className="brand"><span className="brand-mark" aria-hidden="true">D</span><span>Drivebound</span></div>;
}

function Onboarding({ user, onComplete }: { user: DriveboundUser; onComplete: (user: DriveboundUser) => void }) {
  const [step, setStep] = useState(1);
  const [name, setName] = useState(user.display_name ?? "");
  const [libraryName, setLibraryName] = useState("My photo drive");
  const [path, setPath] = useState("/data/imports");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const titleRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    titleRef.current?.focus();
  }, [step]);

  const connectDrive = async () => {
    setBusy(true); setMessage(null);
    try {
      const response = await apiRequest("/api/v1/libraries", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: libraryName, path }),
      });
      const result = await response.json().catch(() => null);
      if (!response.ok && response.status !== 409) { setMessage(result?.detail ?? "Drivebound could not connect that folder."); return; }
      if (response.ok) await apiRequest(`/api/v1/libraries/${result.id}/scan`, { method: "POST" });
      setStep(3);
    } catch {
      setMessage("Drivebound could not reach the storage service. Check the connection and try again.");
    } finally {
      setBusy(false);
    }
  };

  const finish = async () => {
    setBusy(true); setMessage(null);
    try {
      const response = await apiRequest("/api/v1/auth/onboarding", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ display_name: name }),
      });
      if (response.ok) onComplete(await response.json());
      else setMessage("Setup could not be completed. Please try again.");
    } catch {
      setMessage("Setup could not reach Drivebound. Check the connection and try again.");
    } finally {
      setBusy(false);
    }
  };

  return <main className="onboarding-shell" id="main-content">
    <header><Brand /><span>Setup {step} of 3</span></header>
    <section className="onboarding-card">
      <div className="step-track" role="progressbar" aria-label="Account setup progress" aria-valuemin={1} aria-valuemax={3} aria-valuenow={step} aria-valuetext={`Step ${step} of 3`}><span style={{ width: `${step * 33.333}%` }} /></div>
      {step === 1 && <><div className="step-icon" aria-hidden="true">01</div><span className="eyebrow">Make it yours</span><h1 ref={titleRef} tabIndex={-1}>Welcome to Drivebound.</h1><p>This name appears on your private library and connected devices.</p><label>Your name<input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" /></label><button className="primary-button" disabled={!name.trim()} onClick={() => setStep(2)}>Continue</button></>}
      {step === 2 && <><div className="step-icon" aria-hidden="true">02</div><span className="eyebrow">Connect storage</span><h1 ref={titleRef} tabIndex={-1}>Bring your first drive online.</h1><p>Drivebound indexes this folder without moving or modifying the originals.</p><label>Drive name<input value={libraryName} onChange={(e) => setLibraryName(e.target.value)} /></label><label>Server media path<input value={path} onChange={(e) => setPath(e.target.value)} spellCheck={false} /><small>Enter the path mounted inside the Drivebound server or container. The default maps to the project&apos;s data/imports folder.</small></label>{message && <p className="form-error" role="alert">{message}</p>}<div className="button-row"><button className="secondary-button" onClick={() => setStep(3)}>Do this later</button><button className="primary-button" disabled={busy} onClick={() => void connectDrive()}>{busy ? "Connecting…" : "Connect drive"}</button></div></>}
      {step === 3 && <><div className="step-icon success" aria-hidden="true">✓</div><span className="eyebrow">Ready to grow</span><h1 ref={titleRef} tabIndex={-1}>Your private cloud is ready.</h1><p>Add files now, then mount <strong>/data/replicas</strong> on a second physical drive for verified protection.</p>{message && <p className="form-error" role="alert">{message}</p>}<button className="primary-button" disabled={busy} onClick={() => void finish()}>{busy ? "Opening…" : "Open my library"}</button></>}
    </section>
  </main>;
}

export function AuthGate() {
  const [user, setUser] = useState<DriveboundUser | null | undefined>(undefined);
  useEffect(() => { apiRequest("/api/v1/auth/me").then(async (r) => {
    if (!r.ok) { window.location.assign("/auth"); return; }
    setUser(await r.json());
  }).catch(() => window.location.assign("/auth")); }, []);
  if (!user) return <main className="app-loading" id="main-content" aria-busy="true"><div className="depth-orbit" aria-hidden="true"><span><i>D</i></span></div><p role="status">Opening Drivebound…</p></main>;
  if (!user.onboarding_completed_at) return <Onboarding user={user} onComplete={setUser} />;
  return <PhotoLibrary user={user} />;
}
