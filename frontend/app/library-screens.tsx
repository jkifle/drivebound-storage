"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { API_URL, apiRequest } from "./api-client";

export type LibraryView = "photos" | "memories" | "files" | "albums" | "search" | "map";

type MediaItem = {
  id: string;
  mime_type: string;
  taken_at: string | null;
  timeline_at?: string;
  original_url: string;
  thumbnail_url: string | null;
  original_filename?: string | null;
  protection_status?: string;
};

type FileItem = MediaItem & {
  name: string;
  relative_path: string | null;
  file_size: number;
  created_at: string;
  file_modified_at: string | null;
  latitude: number | null;
  longitude: number | null;
};

type Album = {
  id: string;
  name: string;
  description: string | null;
  asset_count: number;
  cover_thumbnail_url: string | null;
  created_at: string;
  role: "owner" | "editor" | "viewer";
};

type MapItem = {
  id: string;
  name: string;
  latitude: number;
  longitude: number;
  taken_at: string | null;
  thumbnail_url: string | null;
};

function url(path: string | null) {
  if (!path) return "";
  return path.startsWith("http") ? path : `${API_URL}${path}`;
}

function formatBytes(value: number) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  if (!value) return "0 B";
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function MediaCard({ item, action }: { item: MediaItem; action?: React.ReactNode }) {
  const title = item.original_filename ?? "Untitled media";
  return <article className="discovery-media-card">
    <a href={url(item.original_url)} target="_blank" rel="noreferrer" aria-label={`Open ${title} in a new tab`}>
      {item.thumbnail_url
        ? <img src={url(item.thumbnail_url)} alt="" loading="lazy" />
        : <span className="media-placeholder" aria-hidden="true">{item.mime_type.startsWith("video/") ? "Video" : "File"}</span>}
    </a>
    <div><strong title={title}>{title}</strong><small>{item.taken_at ? new Date(item.taken_at).toLocaleDateString() : "Date unavailable"}</small></div>
    {action}
  </article>;
}

function FilesScreen() {
  const [items, setItems] = useState<FileItem[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (value = "") => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ limit: "500" });
      if (value.trim()) params.set("query", value.trim());
      const response = await apiRequest(`/api/v1/files?${params}`);
      const result = await response.json().catch(() => null);
      if (!response.ok) throw new Error(result?.detail ?? "Files could not be loaded.");
      setItems(result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Files could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(initial);
  }, [load]);

  return <section className="discovery-screen" aria-labelledby="files-title">
    <header className="screen-heading"><div><span className="eyebrow">Originals, organized</span><h1 id="files-title">Files</h1><p>Browse the original names and folders without losing capture or filesystem metadata.</p></div>
      <form className="screen-search" onSubmit={(event) => { event.preventDefault(); void load(query); }} aria-busy={loading}><label className="sr-only" htmlFor="file-filter">Filter files by filename</label><input id="file-filter" placeholder="Filter by filename" value={query} onChange={(event) => setQuery(event.target.value)} /><button disabled={loading}>{loading ? "Filtering…" : "Filter"}</button></form>
    </header>
    {error && <div className="screen-notice error" role="alert"><span>{error}</span><button onClick={() => void load(query)}>Try again</button></div>}
    <div className="file-table" role="table" aria-label="Stored files" aria-rowcount={items.length + 1}>
      <div role="rowgroup"><div className="file-row file-head" role="row"><span role="columnheader">Name</span><span role="columnheader">Folder</span><span role="columnheader">Date</span><span role="columnheader">Size</span><span role="columnheader">Protection</span></div></div>
      <div role="rowgroup">{items.map((item) => {
        const protection = item.protection_status ?? "unprotected";
        return <div className="file-row" role="row" key={item.id}>
          <span className="file-name" role="cell">{item.thumbnail_url ? <img src={url(item.thumbnail_url)} alt="" loading="lazy" /> : <i aria-hidden="true">F</i>}<a href={url(item.original_url)} target="_blank" rel="noreferrer" aria-label={`Open ${item.name} in a new tab`}>{item.name}</a></span>
          <span role="cell">{item.relative_path?.split(/[\\/]/).slice(0, -1).join("/") || "Library root"}</span>
          <span role="cell">{new Date(item.taken_at ?? item.file_modified_at ?? item.created_at).toLocaleString()}</span>
          <span role="cell">{formatBytes(item.file_size)}</span><span role="cell" className={`status-pill ${protection}`}>{protection}</span>
        </div>;
      })}</div>
    </div>
    {loading && <p className="screen-status" role="status">Loading files…</p>}
    {!loading && !error && !items.length && <div className="screen-empty"><h2>No matching files</h2><p>Try another filename or add files to your library.</p></div>}
  </section>;
}

function SearchScreen() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<MediaItem[]>([]);
  const [searched, setSearched] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const search = async (event: FormEvent) => {
    event.preventDefault();
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ query: query.trim(), limit: "200" });
      const response = await apiRequest(`/api/v1/intelligence/search?${params}`);
      if (response.ok) setResults((await response.json()).map((result: { asset: MediaItem }) => result.asset));
      else {
        const fallback = await apiRequest(`/api/v1/search?${params}`);
        if (!fallback.ok) throw new Error("Search could not be completed.");
        setResults(await fallback.json());
      }
      setSearched(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Search could not be completed.");
    } finally {
      setLoading(false);
    }
  };

  return <section className="discovery-screen search-screen" aria-labelledby="search-title">
    <header className="screen-heading"><div><span className="eyebrow">Meaning, words, and metadata</span><h1 id="search-title">Search</h1><p>Describe a moment, search text visible in an image, or use filenames, dates, cameras, and preserved metadata.</p></div></header>
    <form className="hero-search" onSubmit={search} role="search" aria-busy={loading}><label className="sr-only" htmlFor="library-search">Search your library</label><input id="library-search" placeholder="Try “Canon”, “2025-08”, “video”, or a filename" value={query} onChange={(event) => setQuery(event.target.value)} /><button disabled={loading || !query.trim()}>{loading ? "Searching…" : "Search"}</button></form>
    {error && <p className="screen-notice error" role="alert">{error}</p>}
    <p className="sr-only" role="status">{searched && !loading ? `${results.length} search result${results.length === 1 ? "" : "s"}` : ""}</p>
    <div className="discovery-grid">{results.map((item) => <MediaCard key={item.id} item={item} />)}</div>
    {searched && !loading && !error && !results.length && <div className="screen-empty"><h2>No results found</h2><p>Your search covers both visible file information and embedded media metadata.</p></div>}
  </section>;
}

function MemoriesScreen() {
  const [items, setItems] = useState<MediaItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await apiRequest("/api/v1/memories");
      if (!response.ok) throw new Error("Memories could not be loaded.");
      setItems(await response.json());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Memories could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  return <section className="discovery-screen" aria-labelledby="memories-title"><header className="screen-heading"><div><span className="eyebrow">On this day</span><h1 id="memories-title">Memories</h1><p>Moments from this date in earlier years, selected from preserved capture times.</p></div></header>{error && <div className="screen-notice error" role="alert"><span>{error}</span><button onClick={() => void load()}>Try again</button></div>}<div className="discovery-grid">{items.map((item) => <MediaCard key={item.id} item={item} />)}</div>{loading && <p className="screen-status" role="status">Finding memories…</p>}{!loading && !error && !items.length && <div className="screen-empty"><h2>Your memories will grow here</h2><p>When your library includes media from this date in a previous year, it will appear automatically.</p></div>}</section>;
}

function AlbumsScreen() {
  const [albums, setAlbums] = useState<Album[]>([]);
  const [selected, setSelected] = useState<Album | null>(null);
  const [albumItems, setAlbumItems] = useState<MediaItem[]>([]);
  const [allItems, setAllItems] = useState<MediaItem[]>([]);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [picking, setPicking] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteMessage, setInviteMessage] = useState("");
  const [inviteUrl, setInviteUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);

  const loadAlbums = useCallback(async () => {
    setError(null);
    try {
      const response = await apiRequest("/api/v1/albums");
      if (!response.ok) throw new Error("Albums could not be loaded.");
      setAlbums(await response.json());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Albums could not be loaded.");
    }
  }, []);

  const open = useCallback(async (album: Album) => {
    setSelected(album);
    setBusyAction("open");
    setError(null);
    try {
      const response = await apiRequest(`/api/v1/albums/${album.id}/assets`);
      if (!response.ok) throw new Error("This album could not be opened.");
      setAlbumItems(await response.json());
    } catch (reason) {
      setAlbumItems([]);
      setError(reason instanceof Error ? reason.message : "This album could not be opened.");
    } finally {
      setBusyAction(null);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void loadAlbums(), 0);
    return () => window.clearTimeout(initial);
  }, [loadAlbums]);

  const create = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim()) return;
    setBusyAction("create");
    setError(null);
    try {
      const response = await apiRequest("/api/v1/albums", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim() }) });
      const result = await response.json().catch(() => null);
      if (!response.ok) throw new Error(result?.detail ?? "The album could not be created.");
      setName("");
      setCreating(false);
      await loadAlbums();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The album could not be created.");
    } finally {
      setBusyAction(null);
    }
  };

  const showPicker = async () => {
    if (!selected || selected.role === "viewer") return;
    setBusyAction("picker");
    setError(null);
    try {
      const response = await apiRequest("/api/v1/assets?limit=200");
      if (!response.ok) throw new Error("Media choices could not be loaded.");
      setAllItems((await response.json()).items);
      setPicking(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Media choices could not be loaded.");
    } finally {
      setBusyAction(null);
    }
  };

  const add = async (item: MediaItem) => {
    if (!selected || selected.role === "viewer") return;
    setBusyAction(`add-${item.id}`);
    setError(null);
    try {
      const response = await apiRequest(`/api/v1/albums/${selected.id}/assets/${item.id}`, { method: "POST" });
      if (!response.ok) throw new Error("That item could not be added.");
      await open(selected);
      await loadAlbums();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "That item could not be added.");
    } finally {
      setBusyAction(null);
    }
  };

  const remove = async (item: MediaItem) => {
    if (!selected || selected.role === "viewer") return;
    setBusyAction(`remove-${item.id}`);
    setError(null);
    try {
      const response = await apiRequest(`/api/v1/albums/${selected.id}/assets/${item.id}`, { method: "DELETE" });
      if (!response.ok) throw new Error("That item could not be removed.");
      await open(selected);
      await loadAlbums();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "That item could not be removed.");
    } finally {
      setBusyAction(null);
    }
  };

  const invite = async (event: FormEvent) => {
    event.preventDefault();
    if (!selected || selected.role !== "owner" || !inviteEmail.trim()) return;
    setBusyAction("invite");
    setInviteMessage("");
    setInviteUrl(null);
    try {
      const response = await apiRequest(`/api/v1/albums/${selected.id}/invites`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: inviteEmail.trim(), role: "viewer" }) });
      const result = await response.json().catch(() => null);
      if (!response.ok) throw new Error(result?.detail ?? "Invitation could not be sent.");
      setInviteMessage("Invitation sent.");
      setInviteUrl(result.development_url ?? null);
      setInviteEmail("");
    } catch (reason) {
      setInviteMessage(reason instanceof Error ? reason.message : "Invitation could not be sent.");
    } finally {
      setBusyAction(null);
    }
  };

  if (selected) {
    const canEdit = selected.role !== "viewer";
    return <section className="discovery-screen" aria-labelledby="album-title"><header className="screen-heading album-detail-heading"><div><button className="back-button" onClick={() => { setSelected(null); setPicking(false); setError(null); }}>Back to albums</button><span className="eyebrow">Curated collection</span><h1 id="album-title">{selected.name}</h1><p>{selected.description || `${albumItems.length} items`} · {selected.role}</p></div>{canEdit && <button className="primary-button" disabled={busyAction != null} onClick={() => void showPicker()}>{busyAction === "picker" ? "Loading media…" : "Add media"}</button>}</header>
      {error && <p className="screen-notice error" role="alert">{error}</p>}
      {selected.role === "owner" && <form className="inline-create" onSubmit={invite} aria-busy={busyAction === "invite"}><label className="sr-only" htmlFor="album-invite-email">Collaborator email</label><input id="album-invite-email" type="email" required placeholder="Invite by email" value={inviteEmail} onChange={(event) => setInviteEmail(event.target.value)} autoComplete="email" /><button disabled={busyAction != null}>{busyAction === "invite" ? "Sending…" : "Invite viewer"}</button>{inviteMessage && <small role="status">{inviteMessage}{inviteUrl && <> <a href={inviteUrl}>Open local invitation</a></>}</small>}</form>}
      {picking && <section className="album-picker" aria-label="Choose media for this album"><div><strong>Choose media</strong><button onClick={() => setPicking(false)}>Done</button></div><div className="discovery-grid">{allItems.map((item) => { const added = albumItems.some((current) => current.id === item.id); return <MediaCard key={item.id} item={item} action={<button onClick={() => void add(item)} disabled={busyAction != null || added}>{busyAction === `add-${item.id}` ? "Adding…" : added ? "Added" : "Add"}</button>} />; })}</div></section>}
      <div className="discovery-grid">{albumItems.map((item) => <MediaCard key={item.id} item={item} action={canEdit ? <button onClick={() => void remove(item)} disabled={busyAction != null}>{busyAction === `remove-${item.id}` ? "Removing…" : "Remove"}</button> : undefined} />)}</div>
      {busyAction === "open" && <p className="screen-status" role="status">Opening album…</p>}
      {!busyAction && !error && !albumItems.length && !picking && <div className="screen-empty"><h2>This album is ready</h2><p>{canEdit ? "Add photos, videos, and files without duplicating the originals." : "The album owner has not added anything yet."}</p>{canEdit && <button className="primary-button" onClick={() => void showPicker()}>Choose media</button>}</div>}
    </section>;
  }

  return <section className="discovery-screen" aria-labelledby="albums-title"><header className="screen-heading"><div><span className="eyebrow">Your collections</span><h1 id="albums-title">Albums</h1><p>Group media without moving or duplicating the original files.</p></div><button className="primary-button" disabled={creating || busyAction != null} onClick={() => setCreating(true)}>New album</button></header>
    {error && <div className="screen-notice error" role="alert"><span>{error}</span><button onClick={() => void loadAlbums()}>Try again</button></div>}
    {creating && <form className="inline-create" onSubmit={create} aria-busy={busyAction === "create"}><label className="sr-only" htmlFor="album-name">Album name</label><input id="album-name" required placeholder="Album name" value={name} onChange={(event) => setName(event.target.value)} /><button disabled={busyAction != null}>{busyAction === "create" ? "Creating…" : "Create"}</button><button type="button" disabled={busyAction != null} onClick={() => setCreating(false)}>Cancel</button></form>}
    <div className="album-grid">{albums.map((album) => <button className="album-card" key={album.id} onClick={() => void open(album)} aria-label={`Open ${album.name}, ${album.asset_count} items`}>{album.cover_thumbnail_url ? <img src={url(album.cover_thumbnail_url)} alt="" loading="lazy" /> : <span className="album-placeholder" aria-hidden="true">Album</span>}<div><strong>{album.name}</strong><small>{album.asset_count} items · {album.role}</small></div></button>)}</div>
    {!albums.length && !creating && !error && <div className="screen-empty"><h2>Create your first album</h2><p>Albums reference your originals, so they use virtually no additional storage.</p></div>}
  </section>;
}

function MapScreen() {
  const [items, setItems] = useState<MapItem[]>([]);
  const [selected, setSelected] = useState<MapItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await apiRequest("/api/v1/map?limit=2000");
      if (!response.ok) throw new Error("Mapped media could not be loaded.");
      setItems(await response.json());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Mapped media could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const points = useMemo(() => items.map((item) => {
    const lat = Math.max(-85, Math.min(85, item.latitude));
    const x = ((item.longitude + 180) / 360) * 100;
    const y = (1 - (Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360)) / Math.PI + 1) / 2) * 100;
    return { item, x, y };
  }), [items]);

  return <section className="discovery-screen" aria-labelledby="map-title"><header className="screen-heading"><div><span className="eyebrow">Preserved GPS</span><h1 id="map-title">Map</h1><p>{items.length ? `${items.length} items include location metadata.` : "Photos with embedded GPS coordinates appear here automatically."}</p></div></header>
    {error && <div className="screen-notice error" role="alert"><span>{error}</span><button onClick={() => void load()}>Try again</button></div>}
    <div className="world-map" role="region" aria-label="Map of media locations">{loading && <p className="screen-status map-status" role="status">Loading locations…</p>}<div className="map-grid" aria-hidden="true" />{points.map(({ item, x, y }) => <button key={item.id} className={`map-pin ${selected?.id === item.id ? "active" : ""}`} style={{ left: `${x}%`, top: `${y}%` }} onClick={() => setSelected(item)} aria-label={`${item.name} at ${item.latitude}, ${item.longitude}`} aria-pressed={selected?.id === item.id} />)}
      {selected && <article className="map-card">{selected.thumbnail_url && <img src={url(selected.thumbnail_url)} alt="" loading="lazy" />}<div><strong>{selected.name}</strong><small>{selected.taken_at ? new Date(selected.taken_at).toLocaleString() : "Capture date unavailable"}</small><span>{selected.latitude.toFixed(5)}, {selected.longitude.toFixed(5)}</span><a href={`https://www.openstreetmap.org/?mlat=${selected.latitude}&mlon=${selected.longitude}#map=12/${selected.latitude}/${selected.longitude}`} target="_blank" rel="noreferrer">Open detailed map in a new tab</a></div><button aria-label="Close location" onClick={() => setSelected(null)}>×</button></article>}
    </div>
    {!loading && !error && !items.length && <div className="screen-empty"><h2>No mapped media yet</h2><p>Drivebound never strips GPS from originals. Locations will appear after media containing GPS EXIF is indexed.</p></div>}
  </section>;
}

export function LibraryScreen({ view }: { view: Exclude<LibraryView, "photos"> }) {
  if (view === "memories") return <MemoriesScreen />;
  if (view === "files") return <FilesScreen />;
  if (view === "albums") return <AlbumsScreen />;
  if (view === "search") return <SearchScreen />;
  return <MapScreen />;
}
