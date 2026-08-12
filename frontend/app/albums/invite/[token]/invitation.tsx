"use client";
import { useEffect, useState } from "react";
import { apiRequest } from "../../../api-client";
import SafeLink from "../../../safe-link";
export function AlbumInvitation({ token }: { token: string }) {
  const [message, setMessage] = useState("Accepting your album invitation…");
  useEffect(() => { void apiRequest(`/api/v1/albums/invitations/${token}/accept`, { method: "POST" }).then(async response => setMessage(response.ok ? "You joined the album." : ((await response.json()).detail ?? "This invitation is unavailable."))); }, [token]);
  return <main className="auth-page"><section className="auth-card"><span className="eyebrow">Shared album</span><h1>{message}</h1><p>Shared albums keep one original while giving invited people controlled access.</p><SafeLink className="primary-button" href="/library">Open your library</SafeLink></section></main>;
}
