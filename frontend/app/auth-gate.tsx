"use client";

import { useCallback, useEffect, useRef, useState } from "react";
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

type SetupLibrary = {
  id: string; name: string; status: string; file_count: number;
  last_scanned_at: string | null; error: string | null;
};
export type HostSetup = {
  configured: boolean; can_connect: boolean; folder_name: string | null;
  library: SetupLibrary | null; storage_available: boolean; message: string;
};

function Onboarding({ user, onComplete }: { user: DriveboundUser; onComplete: (user: DriveboundUser) => void }) {
  const [step, setStep] = useState(1);
  const [name, setName] = useState(user.display_name ?? "");
  const [libraryName, setLibraryName] = useState("My photo drive");
  const [hostSetup, setHostSetup] = useState<HostSetup | null>(null);
  const [setupLoading, setSetupLoading] = useState(false);
  const [setupError, setSetupError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const titleRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    titleRef.current?.focus();
  }, [step]);

  const loadHostSetup = useCallback(async () => {
    setSetupLoading(true);
    try {
      const response = await apiRequest("/api/v1/libraries/setup");
      if (!response.ok) throw new Error("Setup status could not be loaded. Check that Drivebound is running on your PC, then retry.");
      setHostSetup(await response.json());
      setSetupError(null);
    } catch (error) {
      setSetupError(error instanceof Error ? error.message : "Setup status could not be loaded. Please retry.");
    } finally { setSetupLoading(false); }
  }, []);

  const scanPending = hostSetup?.library?.status === "queued" || hostSetup?.library?.status === "scanning";
  useEffect(() => {
    if (step !== 2) return;
    // One request at a time; stop polling when the screen is left or scan completes.
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = async () => {
      await loadHostSetup();
      if (!cancelled && scanPending) timer = setTimeout(() => void refresh(), 3000);
    };
    void refresh();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [step, scanPending, loadHostSetup]);

  const connectDrive = async () => {
    setBusy(true); setMessage(null);
    try {
      let library = hostSetup?.library;
      if (!library) {
        const response = await apiRequest("/api/v1/libraries/connect", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: libraryName.trim() }),
        });
        const result = await response.json().catch(() => null);
        if (!response.ok) { setMessage(typeof result?.detail === "string" ? result.detail : "Drivebound could not connect that folder."); return; }
        library = result as SetupLibrary;
      }
      const scan = await apiRequest(`/api/v1/libraries/${library.id}/scan`, { method: "POST" });
      const result = await scan.json().catch(() => null);
      if (!scan.ok || result?.status === "failed") {
        setMessage(typeof result?.detail === "string" ? result.detail : "The import could not start. Your folder is still connected. Retry when the PC and drive are available.");
        await loadHostSetup();
        return;
      }
      setMessage("Import started. Keep your PC and drive connected while Drivebound finds your photos.");
      await loadHostSetup();
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
      {step === 2 && <>
        <div className="step-icon" aria-hidden="true">02</div><span className="eyebrow">Connect storage</span>
        <h1 ref={titleRef} tabIndex={-1}>Bring your first drive online.</h1>
        <p>The photo folder chosen in Windows Setup stays on your drive. Drivebound reads the originals without moving or changing them.</p>
        <p>Only supported photo and video formats are imported; other files are skipped. Available capture dates and location stay with each original.</p>
        {hostSetup?.can_connect && <><label>Drive name<input value={libraryName} onChange={(e) => setLibraryName(e.target.value)} disabled={busy || scanPending} maxLength={255} /></label><p><strong>Selected folder: </strong>{hostSetup.folder_name ?? "Your photo folder"}</p></>}
        {hostSetup && <p role="status">{hostSetup.message}</p>}
        {hostSetup?.library && <p aria-live="polite">{scanPending ? "Import in progress. You can open your library while photos are being indexed." : hostSetup.library.status === "failed" ? "Import paused or failed. Reconnect the selected drive and retry." : hostSetup.library.last_scanned_at ? `${hostSetup.library.file_count.toLocaleString()} supported files found. Previews and media details may still be processing.` : "Folder connected. Start an import to find your photos."}</p>}
        {setupError && <p className="form-error" role="alert">{setupError}</p>}
        {message && <p role="status">{message}</p>}
        <div className="button-row">
          <button className="secondary-button" disabled={busy} onClick={() => { setMessage(null); setStep(3); }}>{scanPending ? "Continue while importing" : hostSetup?.library?.last_scanned_at ? "Continue" : "Set up imports later"}</button>
          {hostSetup?.can_connect && <button className="primary-button" disabled={busy || scanPending || !libraryName.trim() || !hostSetup.storage_available || !!setupError} onClick={() => void connectDrive()}>{busy ? "Connecting…" : scanPending ? "Importing…" : hostSetup.library ? "Retry / scan folder" : "Connect selected folder"}</button>}
          {(setupError || !hostSetup?.storage_available) && <button className="secondary-button" disabled={setupLoading} onClick={() => void loadHostSetup()}>{setupLoading ? "Checking…" : "Retry status check"}</button>}
        </div>
      </>}
      {step === 3 && <><div className="step-icon success" aria-hidden="true">✓</div><span className="eyebrow">Your account</span><h1 ref={titleRef} tabIndex={-1}>Make yourself at home.</h1><p>{scanPending ? "Your import is still running. Photos appear as they are processed." : "You can upload files and check connected drives from your library."} Check Storage for protection-copy and recovery status. A second physical drive is needed for protection against drive failure.</p><p>For access away from home, open Drivebound Remote Access on your PC. Keep the PC awake; automatic startup requires Windows sign-in.</p>{message && <p className="form-error" role="alert">{message}</p>}<div className="button-row"><button className="secondary-button" disabled={busy} onClick={() => { setMessage(null); setStep(2); }}>Back to storage</button><button className="primary-button" disabled={busy} onClick={() => void finish()}>{busy ? "Opening…" : "Open my library"}</button></div></>}
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
