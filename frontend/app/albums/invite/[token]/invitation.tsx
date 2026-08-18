"use client";

import { useCallback, useEffect, useState } from "react";
import { apiRequest } from "../../../api-client";
import SafeLink from "../../../safe-link";

export function AlbumInvitation({ token }: { token: string }) {
  const [message, setMessage] = useState("Accepting your album invitation…");
  const [busy, setBusy] = useState(true);
  const [failed, setFailed] = useState(false);

  const accept = useCallback(async () => {
    setBusy(true);
    setFailed(false);
    setMessage("Accepting your album invitation…");
    try {
      const response = await apiRequest(`/api/v1/albums/invitations/${token}/accept`, { method: "POST" });
      const result = await response.json().catch(() => ({}));
      setFailed(!response.ok);
      setMessage(response.ok ? "You joined the album." : (result.detail ?? "This invitation is unavailable."));
    } catch {
      setFailed(true);
      setMessage("Drivebound could not reach this invitation. Check the connection and try again.");
    } finally {
      setBusy(false);
    }
  }, [token]);

  useEffect(() => {
    const timer = window.setTimeout(() => void accept(), 0);
    return () => window.clearTimeout(timer);
  }, [accept]);

  return <main className="centered-page" id="main-content"><section className="auth-card" aria-busy={busy}>
    <span className="eyebrow">Shared album</span><h1 role={failed ? "alert" : "status"}>{message}</h1>
    <p>Shared albums keep one original while giving invited people controlled access.</p>
    {failed && <button className="secondary-button" disabled={busy} onClick={() => void accept()}>{busy ? "Trying again…" : "Try again"}</button>}
    <SafeLink className="primary-button" href="/library">Open your library</SafeLink>
  </section></main>;
}
