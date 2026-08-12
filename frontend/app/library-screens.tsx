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
  return <article className="discovery-media-card">
    <a href={url(item.original_url)} target="_blank" rel="noreferrer">
      {item.thumbnail_url
        ? <img src={url(item.thumbnail_url)} alt="" loading="lazy" />
        : <span className="media-placeholder">{item.mime_type.startsWith("video/") ? "Video" : "File"}</span>}
    </a>
    <div><strong title={item.original_filename ?? "Media"}>{item.original_filename ?? "Untitled media"}</strong>
      <small>{item.taken_at ? new Date(item.taken_at).toLocaleDateString() : "Date unavailable"}</small>
    </div>
    {action}
  </article>;
}

function FilesScreen() {
  const [items, setItems] = useState<FileItem[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const load = useCallback(async (value = "") => {
    setLoading(true);
    const params = new URLSearchParams({ limit: "500" });
    if (value.trim()) params.set("query", value.trim());
    const response = await apiRequest(`/api/v1/files?${params}`);
    if (response.ok) setItems(await response.json());
    setLoading(false);
  }, []);
  useEffect(() => { const initial = window.setTimeout(() => void load(), 0); return () => window.clearTimeout(initial); }, [load]);
  return <section className="discovery-screen">
    <header className="screen-heading"><div><span className="eyebrow">Originals, organized</span><h1>Files</h1><p>Browse the original names and folders without losing capture or filesystem metadata.</p></div>
      <form className="screen-search" onSubmit={(event) => { event.preventDefault(); void load(query); }}><input aria-label="Filter files" placeholder="Filter by filename" value={query} onChange={(event) => setQuery(event.target.value)} /><button>Filter</button></form>
    </header>
    <div className="file-table" role="table" aria-label="Stored files">
      <div className="file-row file-head" role="row"><span>Name</span><span>Folder</span><span>Date</span><span>Size</span><span>Protection</span></div>
      {items.map((item) => <a className="file-row" role="row" key={item.id} href={url(item.original_url)} target="_blank" rel="noreferrer">
        <span className="file-name">{item.thumbnail_url ? <img src={url(item.thumbnail_url)} alt="" /> : <i>F</i>}<b>{item.name}</b></span>
        <span>{item.relative_path?.split(/[\\/]/).slice(0, -1).join("/") || "Library root"}</span>
        <span>{new Date(item.taken_at ?? item.file_modified_at ?? item.created_at).toLocaleString()}</span>
        <span>{formatBytes(item.file_size)}</span><span className={`status-pill ${item.protection_status}`}>{item.protection_status}</span>
      </a>)}
    </div>
    {!loading && !items.length && <div className="screen-empty"><h2>No matching files</h2><p>Try another filename or add files to your library.</p></div>}
  </section>;
}

function SearchScreen() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<MediaItem[]>([]);
  const [searched, setSearched] = useState(false);
  const search = async (event: FormEvent) => {
    event.preventDefault();
    if (!query.trim()) return;
    const response = await apiRequest(`/api/v1/intelligence/search?${new URLSearchParams({ query: query.trim(), limit: "200" })}`);
    if (response.ok) setResults((await response.json()).map((result: { asset: MediaItem }) => result.asset));
    else {
      const fallback = await apiRequest(`/api/v1/search?${new URLSearchParams({ query: query.trim(), limit: "200" })}`);
      setResults(fallback.ok ? await fallback.json() : []);
    }
    setSearched(true);
  };
  return <section className="discovery-screen search-screen">
    <header className="screen-heading"><div><span className="eyebrow">Meaning, words, and metadata</span><h1>Search</h1><p>Describe a moment, search text visible in an image, or use filenames, dates, cameras, and preserved metadata.</p></div></header>
    <form className="hero-search" onSubmit={search}><input aria-label="Search your library" placeholder="Try “Canon”, “2025-08”, “video”, or a filename" value={query} onChange={(event) => setQuery(event.target.value)} /><button>Search</button></form>
    <div className="discovery-grid">{results.map((item) => <MediaCard key={item.id} item={item} />)}</div>
    {searched && !results.length && <div className="screen-empty"><h2>No results found</h2><p>Your search covers both visible file information and embedded media metadata.</p></div>}
  </section>;
}

function MemoriesScreen() {
  const [items, setItems] = useState<MediaItem[]>([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => { void apiRequest("/api/v1/memories").then(async response => { if (response.ok) setItems(await response.json()); }).finally(() => setLoading(false)); }, []);
  return <section className="discovery-screen"><header className="screen-heading"><div><span className="eyebrow">On this day</span><h1>Memories</h1><p>Moments from this date in earlier years, selected from preserved capture times.</p></div></header><div className="discovery-grid">{items.map(item => <MediaCard key={item.id} item={item} />)}</div>{!loading && !items.length && <div className="screen-empty"><h2>Your memories will grow here</h2><p>When your library includes media from this date in a previous year, it will appear automatically.</p></div>}</section>;
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
  const loadAlbums = useCallback(async () => {
    const response = await apiRequest("/api/v1/albums");
    if (response.ok) setAlbums(await response.json());
  }, []);
  const open = useCallback(async (album: Album) => {
    setSelected(album);
    const response = await apiRequest(`/api/v1/albums/${album.id}/assets`);
    setAlbumItems(response.ok ? await response.json() : []);
  }, []);
  useEffect(() => { const initial = window.setTimeout(() => void loadAlbums(), 0); return () => window.clearTimeout(initial); }, [loadAlbums]);
  const create = async (event: FormEvent) => {
    event.preventDefault(); if (!name.trim()) return;
    const response = await apiRequest("/api/v1/albums", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim() }) });
    if (response.ok) { setName(""); setCreating(false); await loadAlbums(); }
  };
  const showPicker = async () => {
    const response = await apiRequest("/api/v1/assets?limit=200");
    if (response.ok) setAllItems((await response.json()).items);
    setPicking(true);
  };
  const add = async (item: MediaItem) => {
    if (!selected) return;
    const response = await apiRequest(`/api/v1/albums/${selected.id}/assets/${item.id}`, { method: "POST" });
    if (response.ok) { await open(selected); await loadAlbums(); }
  };
  const remove = async (item: MediaItem) => {
    if (!selected) return;
    await apiRequest(`/api/v1/albums/${selected.id}/assets/${item.id}`, { method: "DELETE" });
    await open(selected); await loadAlbums();
  };
  const invite = async (event: FormEvent) => {
    event.preventDefault(); if (!selected || !inviteEmail.trim()) return;
    const response = await apiRequest(`/api/v1/albums/${selected.id}/invites`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: inviteEmail.trim(), role: "viewer" }) });
    const result = await response.json().catch(() => null);
    setInviteMessage(response.ok ? (result.development_url ? `Local invite link: ${result.development_url}` : "Invitation sent.") : (result?.detail ?? "Invitation could not be sent."));
    if (response.ok) setInviteEmail("");
  };
  if (selected) return <section className="discovery-screen"><header className="screen-heading album-detail-heading"><div><button className="back-button" onClick={() => { setSelected(null); setPicking(false); }}>Back to albums</button><span className="eyebrow">Curated collection</span><h1>{selected.name}</h1><p>{selected.description || `${albumItems.length} items`}</p></div><button className="primary-button" onClick={() => void showPicker()}>Add media</button></header>
    {selected.role === "owner" && <form className="inline-create" onSubmit={invite}><input type="email" aria-label="Collaborator email" placeholder="Invite by email" value={inviteEmail} onChange={event => setInviteEmail(event.target.value)} /><button>Invite viewer</button>{inviteMessage && <small>{inviteMessage}</small>}</form>}
    {picking && <div className="album-picker"><div><strong>Choose media</strong><button onClick={() => setPicking(false)}>Done</button></div><div className="discovery-grid">{allItems.map((item) => <MediaCard key={item.id} item={item} action={<button onClick={() => void add(item)} disabled={albumItems.some((current) => current.id === item.id)}>{albumItems.some((current) => current.id === item.id) ? "Added" : "Add"}</button>} />)}</div></div>}
    <div className="discovery-grid">{albumItems.map((item) => <MediaCard key={item.id} item={item} action={<button onClick={() => void remove(item)}>Remove</button>} />)}</div>
    {!albumItems.length && !picking && <div className="screen-empty"><h2>This album is ready</h2><p>Add photos and videos without duplicating the originals.</p><button className="primary-button" onClick={() => void showPicker()}>Choose media</button></div>}
  </section>;
  return <section className="discovery-screen"><header className="screen-heading"><div><span className="eyebrow">Your collections</span><h1>Albums</h1><p>Group media without moving or duplicating the original files.</p></div><button className="primary-button" onClick={() => setCreating(true)}>New album</button></header>
    {creating && <form className="inline-create" onSubmit={create}><input aria-label="Album name" placeholder="Album name" value={name} onChange={(event) => setName(event.target.value)} /><button>Create</button><button type="button" onClick={() => setCreating(false)}>Cancel</button></form>}
    <div className="album-grid">{albums.map((album) => <button className="album-card" key={album.id} onClick={() => void open(album)}>{album.cover_thumbnail_url ? <img src={url(album.cover_thumbnail_url)} alt="" /> : <span className="album-placeholder">Album</span>}<div><strong>{album.name}</strong><small>{album.asset_count} items</small></div></button>)}</div>
    {!albums.length && !creating && <div className="screen-empty"><h2>Create your first album</h2><p>Albums reference your originals, so they use virtually no additional storage.</p></div>}
  </section>;
}

function MapScreen() {
  const [items, setItems] = useState<MapItem[]>([]);
  const [selected, setSelected] = useState<MapItem | null>(null);
  useEffect(() => { void apiRequest("/api/v1/map?limit=2000").then(async (response) => { if (response.ok) setItems(await response.json()); }); }, []);
  const points = useMemo(() => items.map((item) => {
    const lat = Math.max(-85, Math.min(85, item.latitude));
    const x = ((item.longitude + 180) / 360) * 100;
    const y = (1 - (Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360)) / Math.PI + 1) / 2) * 100;
    return { item, x, y };
  }), [items]);
  return <section className="discovery-screen"><header className="screen-heading"><div><span className="eyebrow">Preserved GPS</span><h1>Map</h1><p>{items.length ? `${items.length} items include location metadata.` : "Photos with embedded GPS coordinates appear here automatically."}</p></div></header>
    <div className="world-map" aria-label="Map of media locations"><div className="map-grid" />{points.map(({ item, x, y }) => <button key={item.id} className={`map-pin ${selected?.id === item.id ? "active" : ""}`} style={{ left: `${x}%`, top: `${y}%` }} onClick={() => setSelected(item)} aria-label={`${item.name} at ${item.latitude}, ${item.longitude}`} />)}
      {selected && <article className="map-card">{selected.thumbnail_url && <img src={url(selected.thumbnail_url)} alt="" />}<div><strong>{selected.name}</strong><small>{selected.taken_at ? new Date(selected.taken_at).toLocaleString() : "Capture date unavailable"}</small><span>{selected.latitude.toFixed(5)}, {selected.longitude.toFixed(5)}</span><a href={`https://www.openstreetmap.org/?mlat=${selected.latitude}&mlon=${selected.longitude}#map=12/${selected.latitude}/${selected.longitude}`} target="_blank" rel="noreferrer">Open detailed map</a></div><button aria-label="Close location" onClick={() => setSelected(null)}>×</button></article>}
    </div>
    {!items.length && <div className="screen-empty"><h2>No mapped media yet</h2><p>Drivebound never strips GPS from originals. Locations will appear after media containing GPS EXIF is indexed.</p></div>}
  </section>;
}

export function LibraryScreen({ view }: { view: Exclude<LibraryView, "photos"> }) {
  if (view === "memories") return <MemoriesScreen />;
  if (view === "files") return <FilesScreen />;
  if (view === "albums") return <AlbumsScreen />;
  if (view === "search") return <SearchScreen />;
  return <MapScreen />;
}
