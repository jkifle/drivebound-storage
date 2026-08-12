"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "../../safe-link";

const API_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type SharedAsset = {
  filename: string | null;
  mime_type: string;
  expires_at: string | null;
  content_url: string;
};

export function ShareView({ token }: { token: string }) {
  const [password, setPassword] = useState("");
  const [asset, setAsset] = useState<SharedAsset | null>(null);
  const [contentUrl, setContentUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const unlock = async (event?: FormEvent) => {
    event?.preventDefault();
    setError(null);
    const headers = password ? { "X-Share-Password": password } : undefined;
    const metadata = await fetch(`${API_URL}/api/v1/shares/public/${token}`, { headers });
    if (!metadata.ok) {
      setError(metadata.status === 401 ? "Enter the link password to continue." : "This link is unavailable or has expired.");
      return;
    }
    const result: SharedAsset = await metadata.json();
    const content = await fetch(`${API_URL}${result.content_url}`, { headers });
    if (!content.ok) {
      setError("The shared file could not be loaded.");
      return;
    }
    setAsset(result);
    setContentUrl(URL.createObjectURL(await content.blob()));
  };

  useEffect(() => {
    const initial = window.setTimeout(() => void unlock(), 0);
    return () => window.clearTimeout(initial);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => { if (contentUrl) URL.revokeObjectURL(contentUrl); }, [contentUrl]);

  return (
    <main className="share-page">
      <header><Link href="/"><span className="brand-mark">D</span> Drivebound</Link><span>Private share</span></header>
      <section>
        {contentUrl && asset ? (
          <>
            {asset.mime_type.startsWith("video/") ? (
              // User-provided videos do not have an associated captions source.
              // eslint-disable-next-line jsx-a11y/media-has-caption
              <video src={contentUrl} controls />
            ) : <img src={contentUrl} alt={asset.filename ?? "Shared memory"} />}
            <div><h1>{asset.filename ?? "Shared memory"}</h1><p>{asset.expires_at ? `Available until ${new Date(asset.expires_at).toLocaleString()}` : "No expiration"}</p></div>
          </>
        ) : (
          <form onSubmit={(event) => void unlock(event)}>
            <span className="eyebrow">A private Drivebound link</span>
            <h1>Open shared file</h1>
            <p>{error ?? "Loading the file securelyâ€¦"}</p>
            {error?.startsWith("Enter") && <><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Password" aria-label="Share password" /><button>Unlock</button></>}
          </form>
        )}
      </section>
    </main>
  );
}
