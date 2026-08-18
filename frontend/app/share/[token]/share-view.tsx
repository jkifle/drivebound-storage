"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "../../safe-link";

const API_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type SharedAsset = {
  filename: string | null;
  mime_type: string;
  expires_at: string | null;
  content_url: string;
  allow_download: boolean;
};

export function ShareView({ token }: { token: string }) {
  const [password, setPassword] = useState("");
  const [asset, setAsset] = useState<SharedAsset | null>(null);
  const [contentUrl, setContentUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);

  const unlock = async (event?: FormEvent) => {
    event?.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const headers = password ? { "X-Share-Password": password } : undefined;
      const metadata = await fetch(`${API_URL}/api/v1/shares/public/${token}`, { headers });
      if (!metadata.ok) {
        setError(metadata.status === 401 ? "Enter the link password to continue." : "This link is unavailable or has expired.");
        return;
      }
      const result: SharedAsset = await metadata.json();
      const contentEndpoint = result.content_url.startsWith("http") ? result.content_url : `${API_URL}${result.content_url}`;
      const content = await fetch(contentEndpoint, { headers });
      if (!content.ok) {
        setError("The shared file could not be loaded.");
        return;
      }
      setAsset(result);
      setContentUrl(URL.createObjectURL(await content.blob()));
    } catch {
      setError("Drivebound could not reach this shared file. Check the connection and try again.");
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    const initial = window.setTimeout(() => void unlock(), 0);
    return () => window.clearTimeout(initial);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => { if (contentUrl) URL.revokeObjectURL(contentUrl); }, [contentUrl]);

  return (
    <main className="share-page" id="main-content">
      <header><Link href="/"><span className="brand-mark" aria-hidden="true">D</span> Drivebound</Link><span>Private share</span></header>
      <section aria-busy={busy}>
        {contentUrl && asset ? (
          <>
            {asset.mime_type.startsWith("video/") ? (
              // User-provided videos do not have an associated captions source.
              // eslint-disable-next-line jsx-a11y/media-has-caption
              <video src={contentUrl} controls />
            ) : <img src={contentUrl} alt={asset.filename ?? "Shared memory"} />}
            <div><div><h1>{asset.filename ?? "Shared memory"}</h1><p>{asset.expires_at ? `Available until ${new Date(asset.expires_at).toLocaleString()}` : "No expiration"}</p></div>{asset.allow_download && <a className="viewer-download" href={contentUrl} download={asset.filename ?? "drivebound-file"}>Download original</a>}</div>
          </>
        ) : (
          <form onSubmit={(event) => void unlock(event)}>
            <span className="eyebrow">A private Drivebound link</span>
            <h1>Open shared file</h1>
            <p role={error ? "alert" : "status"}>{error ?? "Loading the file securely…"}</p>
            {error?.startsWith("Enter") && <><label>Share password<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></label><button disabled={busy}>{busy ? "Unlocking…" : "Unlock"}</button></>}
            {error && !error.startsWith("Enter") && <button type="button" disabled={busy} onClick={() => void unlock()}>{busy ? "Trying again…" : "Try again"}</button>}
          </form>
        )}
      </section>
    </main>
  );
}
