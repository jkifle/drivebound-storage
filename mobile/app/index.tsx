import { useCallback, useEffect, useMemo, useState } from "react";
import { ActivityIndicator, Alert, Image, Linking, Modal, Platform, SafeAreaView, ScrollView, Share, StyleSheet, Switch, Text, TextInput, TouchableOpacity, View } from "react-native";
import { VideoView, useVideoPlayer } from "expo-video";

import { type Album, type MapAsset, type TimelineAsset, albumAssets, albums, cachedMediaUri, clearConnection, createShare, isConnected, mapAssets, mediaUrl, memories, restoreToPhone, search, timeline } from "../src/api";
import { configureDevice, queueSummary, runBackup, type BackupProgress } from "../src/backup";
import { setAutomaticBackup } from "../src/background";
import { configureNotifications } from "../src/notifications";
import { defaultBackupPolicy, loadBackupPolicy, saveBackupPolicy, type BackupPolicy } from "../src/policies";
import { retryFailedTransfers } from "../src/queue";

type Tab = "library" | "discover" | "backup" | "settings";

function AuthenticatedImage({ path, style }: { path: string | null; style?: object }) {
  const [source, setSource] = useState<{ uri: string } | null>(null);
  useEffect(() => {
    let active = true;
    setSource(null);
    if (path) cachedMediaUri(path).then(uri => active && setSource({ uri })).catch(() => active && setSource(null));
    return () => { active = false; };
  }, [path]);
  if (!source) return <View style={[styles.placeholder, style]}><Text style={styles.placeholderText}>◌</Text></View>;
  return <Image source={source} style={[styles.image, style]} resizeMode="cover" />;
}

function VideoMedia({ path }: { path: string }) {
  const [source, setSource] = useState<{ uri: string; headers: Record<string, string> } | null>(null);
  useEffect(() => { let active = true; mediaUrl(path).then(value => active && setSource(value)).catch(() => active && setSource(null)); return () => { active = false; }; }, [path]);
  const player = useVideoPlayer(source, player => { player.loop = false; });
  if (!source) return <View style={styles.viewerLoading}><ActivityIndicator color="#fff" /></View>;
  return <VideoView style={styles.viewerMedia} player={player} nativeControls />;
}

function AssetCard({ asset, onPress }: { asset: TimelineAsset; onPress: (asset: TimelineAsset) => void }) {
  return <TouchableOpacity style={styles.assetCard} onPress={() => onPress(asset)} accessibilityLabel={`Open ${asset.original_filename || "media"}`}>
    <AuthenticatedImage path={asset.thumbnail_url ?? asset.original_url} style={styles.assetImage} />
    {asset.mime_type.startsWith("video/") && <View style={styles.videoBadge}><Text style={styles.videoBadgeText}>▶</Text></View>}
    <Text numberOfLines={1} style={styles.assetName}>{asset.original_filename || "Untitled media"}</Text>
  </TouchableOpacity>;
}

function AssetGrid({ assets, onSelect }: { assets: TimelineAsset[]; onSelect: (asset: TimelineAsset) => void }) {
  if (!assets.length) return <Text style={styles.empty}>Nothing here yet.</Text>;
  return <View style={styles.grid}>{assets.map(asset => <AssetCard key={asset.id} asset={asset} onPress={onSelect} />)}</View>;
}

function Viewer({ asset, onClose }: { asset: TimelineAsset | null; onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  if (!asset) return null;
  const shareAsset = async () => {
    setBusy(true);
    try { const result = await createShare(asset.id); await Share.share({ message: result.url, url: result.url, title: asset.original_filename || "Drivebound media" }); }
    catch (error) { Alert.alert("Could not create share", String(error)); }
    finally { setBusy(false); }
  };
  const restore = async () => {
    setBusy(true);
    try { await restoreToPhone(asset); Alert.alert("Saved to your phone", "The original was added to your photo library."); }
    catch (error) { Alert.alert("Restore failed", String(error)); }
    finally { setBusy(false); }
  };
  return <Modal visible animationType="slide" onRequestClose={onClose}><SafeAreaView style={styles.viewerSafe}>
    <View style={styles.viewerHeader}><TouchableOpacity onPress={onClose}><Text style={styles.viewerClose}>Close</Text></TouchableOpacity><Text numberOfLines={1} style={styles.viewerTitle}>{asset.original_filename || "Media"}</Text><View style={{ width: 42 }} /></View>
    <View style={styles.viewerContent}>{asset.mime_type.startsWith("video/") ? <VideoMedia path={asset.original_url} /> : <AuthenticatedImage path={asset.original_url} style={styles.viewerMedia} />}</View>
    <View style={styles.viewerActions}><TouchableOpacity disabled={busy} style={styles.secondaryButton} onPress={restore}><Text style={styles.secondaryButtonText}>Save to phone</Text></TouchableOpacity><TouchableOpacity disabled={busy} style={styles.primaryButton} onPress={shareAsset}><Text style={styles.primaryButtonText}>{busy ? "Working…" : "Share"}</Text></TouchableOpacity></View>
  </SafeAreaView></Modal>;
}

export default function Home() {
  const [connected, setConnected] = useState<boolean | null>(null); const [tab, setTab] = useState<Tab>("library");
  const [server, setServer] = useState(Platform.OS === "android" ? "http://10.0.2.2:8000" : "http://localhost:8000"); const [email, setEmail] = useState(""); const [password, setPassword] = useState(""); const [otp, setOtp] = useState("");
  const [message, setMessage] = useState("Connect this phone to begin."); const [library, setLibrary] = useState<TimelineAsset[]>([]); const [selected, setSelected] = useState<TimelineAsset | null>(null); const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState(""); const [results, setResults] = useState<TimelineAsset[]>([]); const [memoryItems, setMemoryItems] = useState<TimelineAsset[]>([]); const [albumItems, setAlbumItems] = useState<Album[]>([]); const [activeAlbum, setActiveAlbum] = useState<Album | null>(null); const [mapItems, setMapItems] = useState<MapAsset[]>([]);
  const [progress, setProgress] = useState<BackupProgress | null>(null); const [summary, setSummary] = useState({ pending: 0, failed: 0, complete: 0 }); const [policy, setPolicy] = useState<BackupPolicy>(defaultBackupPolicy);

  const refreshQueue = useCallback(async () => setSummary(await queueSummary()), []);
  const refreshLibrary = useCallback(async () => { setLoading(true); try { const page = await timeline(); setLibrary(page.items); } catch (error) { setMessage(String(error)); } finally { setLoading(false); } }, []);
  const refreshDiscover = useCallback(async () => { setLoading(true); try { const [nextAlbums, nextMemories, nextMap] = await Promise.all([albums(), memories(), mapAssets()]); setAlbumItems(nextAlbums); setMemoryItems(nextMemories); setMapItems(nextMap); } catch (error) { setMessage(String(error)); } finally { setLoading(false); } }, []);

  useEffect(() => { isConnected().then(value => { setConnected(value); if (value) { refreshLibrary(); refreshQueue(); loadBackupPolicy().then(setPolicy); configureNotifications(); } }); }, [refreshLibrary, refreshQueue]);
  useEffect(() => { if (connected && tab === "discover") refreshDiscover(); }, [connected, tab, refreshDiscover]);

  const connect = async () => { try { await configureDevice(server, email, password, "My phone", otp || undefined); setConnected(true); setMessage("Connected. Your media library is ready."); await Promise.all([refreshLibrary(), refreshQueue(), configureNotifications(), loadBackupPolicy().then(setPolicy)]); } catch (error) { setMessage(String(error)); } };
  const runNow = async () => { setLoading(true); try { const outcome = await runBackup(setProgress); setProgress(outcome); setMessage(outcome.blocked || `Backup complete: ${outcome.uploaded} uploaded, ${outcome.skipped} already protected.`); } catch (error) { setMessage(String(error)); } finally { setLoading(false); refreshQueue(); } };
  const savePolicyChange = async (change: Partial<BackupPolicy>) => { const next = { ...policy, ...change }; setPolicy(next); await saveBackupPolicy(next); await setAutomaticBackup(next.automatic); };
  const performSearch = async () => { setLoading(true); try { setActiveAlbum(null); setResults(await search(query)); } catch (error) { setMessage(String(error)); } finally { setLoading(false); } };
  const openAlbum = async (album: Album) => { setLoading(true); try { setActiveAlbum(album); setResults(await albumAssets(album.id)); } catch (error) { setMessage(String(error)); } finally { setLoading(false); } };
  const mappedPlaces = useMemo(() => mapItems.slice(0, 100), [mapItems]);

  if (connected === null) return <SafeAreaView style={styles.safe}><ActivityIndicator color="#2266d5" /></SafeAreaView>;
  if (!connected) return <SafeAreaView style={styles.safe}><ScrollView contentContainerStyle={styles.loginWrap}><View style={styles.card}><Text style={styles.brand}>Drivebound</Text><Text style={styles.title}>Your media, on your storage.</Text><Text style={styles.copy}>{message}</Text>
    <TextInput style={styles.input} value={server} onChangeText={setServer} autoCapitalize="none" placeholder="Server URL" /><Text style={styles.serverHint}>{Platform.OS === "android" ? "Emulator: 10.0.2.2 · USB device: adb reverse" : "Use your Drivebound HTTPS or LAN address"}</Text>
    <TextInput style={styles.input} value={email} onChangeText={setEmail} autoCapitalize="none" placeholder="Email" keyboardType="email-address" /><TextInput style={styles.input} value={password} onChangeText={setPassword} secureTextEntry placeholder="Password" /><TextInput style={styles.input} value={otp} onChangeText={setOtp} keyboardType="number-pad" placeholder="Authenticator code (if enabled)" />
    <TouchableOpacity style={styles.primaryButton} onPress={connect}><Text style={styles.primaryButtonText}>Connect device</Text></TouchableOpacity>
  </View></ScrollView></SafeAreaView>;

  return <SafeAreaView style={styles.safe}><Viewer asset={selected} onClose={() => setSelected(null)} /><View style={styles.appHeader}><View><Text style={styles.brand}>Drivebound</Text><Text style={styles.headerTitle}>{tab === "library" ? "Library" : tab === "discover" ? "Discover" : tab === "backup" ? "Backup" : "Settings"}</Text></View>{loading && <ActivityIndicator color="#2266d5" />}</View>
    <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
      {message !== "Connected. Your media library is ready." && <Text style={styles.status}>{message}</Text>}
      {tab === "library" && <><View style={styles.sectionHeader}><Text style={styles.sectionTitle}>All media</Text><TouchableOpacity onPress={refreshLibrary}><Text style={styles.textButton}>Refresh</Text></TouchableOpacity></View><AssetGrid assets={library} onSelect={setSelected} /></>}
      {tab === "discover" && <><Text style={styles.sectionTitle}>{activeAlbum ? activeAlbum.name : "Search"}</Text><View style={styles.searchRow}><TextInput style={[styles.input, styles.searchInput]} value={query} onChangeText={setQuery} placeholder="Search people, places, text…" onSubmitEditing={performSearch} /><TouchableOpacity style={styles.searchButton} onPress={performSearch}><Text style={styles.primaryButtonText}>Search</Text></TouchableOpacity></View>{(results.length > 0 || activeAlbum) && <><TouchableOpacity onPress={() => { setResults([]); setActiveAlbum(null); }}><Text style={styles.textButton}>Back to discovery</Text></TouchableOpacity><AssetGrid assets={results} onSelect={setSelected} /></>}{!results.length && !activeAlbum && <><Text style={styles.subheading}>Albums</Text><ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.horizontal}>{albumItems.map(album => <TouchableOpacity key={album.id} style={styles.albumCard} onPress={() => openAlbum(album)}><AuthenticatedImage path={album.cover_thumbnail_url} style={styles.albumImage} /><Text style={styles.albumName}>{album.name}</Text><Text style={styles.albumMeta}>{album.asset_count} items · {album.role}</Text></TouchableOpacity>)}</ScrollView><Text style={styles.subheading}>Memories</Text><AssetGrid assets={memoryItems} onSelect={setSelected} /><Text style={styles.subheading}>Map</Text>{mappedPlaces.map(place => <TouchableOpacity key={place.id} style={styles.place} onPress={() => Linking.openURL(`https://www.google.com/maps/search/?api=1&query=${place.latitude},${place.longitude}`)}><AuthenticatedImage path={place.thumbnail_url} style={styles.placeImage} /><View style={styles.placeText}><Text style={styles.placeName}>{place.name}</Text><Text style={styles.albumMeta}>{place.latitude.toFixed(4)}, {place.longitude.toFixed(4)}</Text></View><Text style={styles.textButton}>Open map</Text></TouchableOpacity>)}</>}</>}
      {tab === "backup" && <><Text style={styles.copy}>Your transfer queue is durable: unfinished uploads resume after the app or device restarts.</Text><View style={styles.statsRow}><Stat value={summary.pending} label="Pending" /><Stat value={summary.complete} label="Protected" /><Stat value={summary.failed} label="Needs attention" /></View><TouchableOpacity style={styles.primaryButton} onPress={runNow}><Text style={styles.primaryButtonText}>{loading ? "Backing up…" : "Back up now"}</Text></TouchableOpacity>{summary.failed > 0 && <TouchableOpacity style={styles.secondaryButton} onPress={async () => { await retryFailedTransfers(); refreshQueue(); }}><Text style={styles.secondaryButtonText}>Retry failed media</Text></TouchableOpacity>}{progress && <Text style={styles.progress}>{progress.scanned} scanned · {progress.queued} queued · {progress.uploaded} uploaded · {progress.skipped} already safe · {progress.failed} failed{progress.current ? `\n${progress.current}` : ""}</Text>}</>}
      {tab === "settings" && <><Text style={styles.subheading}>Automatic backup</Text><Setting label="Automatic backup" detail="Lets the operating system run queued backups." value={policy.automatic} onChange={value => savePolicyChange({ automatic: value })} /><Setting label="Wi-Fi only" detail="Avoid using cellular data." value={policy.wifiOnly} onChange={value => savePolicyChange({ wifiOnly: value })} /><Setting label="Only while charging" detail="Wait until a charger is connected." value={policy.chargingOnly} onChange={value => savePolicyChange({ chargingOnly: value })} /><Text style={styles.subheading}>Transfer limits</Text><Text style={styles.fieldLabel}>Bandwidth limit in Kbps (0 is unlimited)</Text><TextInput style={styles.input} value={String(policy.bandwidthKbps || "")} onChangeText={value => savePolicyChange({ bandwidthKbps: Number(value.replace(/\D/g, "")) || 0 })} keyboardType="number-pad" placeholder="0" /><Text style={styles.fieldLabel}>Schedule (same start and end means anytime)</Text><View style={styles.hourRow}><TextInput style={[styles.input, styles.hourInput]} value={String(policy.scheduleStartHour)} onChangeText={value => savePolicyChange({ scheduleStartHour: Number(value) || 0 })} keyboardType="number-pad" /><Text style={styles.hourSeparator}>to</Text><TextInput style={[styles.input, styles.hourInput]} value={String(policy.scheduleEndHour)} onChangeText={value => savePolicyChange({ scheduleEndHour: Number(value) || 0 })} keyboardType="number-pad" /></View><TouchableOpacity style={styles.secondaryButton} onPress={async () => { await clearConnection(); setConnected(false); }}><Text style={styles.secondaryButtonText}>Disconnect this phone</Text></TouchableOpacity></>}
    </ScrollView>
    <View style={styles.tabs}>{([ ["library", "Library"], ["discover", "Discover"], ["backup", "Backup"], ["settings", "Settings"] ] as [Tab, string][]).map(([value, label]) => <TouchableOpacity key={value} style={styles.tab} onPress={() => setTab(value)}><Text style={[styles.tabText, tab === value && styles.tabTextActive]}>{label}</Text></TouchableOpacity>)}</View>
  </SafeAreaView>;
}

function Stat({ value, label }: { value: number; label: string }) { return <View style={styles.stat}><Text style={styles.statValue}>{value}</Text><Text style={styles.statLabel}>{label}</Text></View>; }
function Setting({ label, detail, value, onChange }: { label: string; detail: string; value: boolean; onChange: (value: boolean) => void }) { return <View style={styles.setting}><View style={styles.settingText}><Text style={styles.settingLabel}>{label}</Text><Text style={styles.settingDetail}>{detail}</Text></View><Switch value={value} onValueChange={onChange} trackColor={{ true: "#7da8f0" }} /></View>; }

const styles = StyleSheet.create({
  safe:{ flex:1, backgroundColor:"#e8edf3" }, loginWrap:{ flexGrow:1, justifyContent:"center", padding:20 }, card:{ padding:28, borderRadius:28, backgroundColor:"#e8edf3", shadowColor:"#77889b", shadowOpacity:.35, shadowRadius:18, elevation:8 }, appHeader:{ flexDirection:"row", justifyContent:"space-between", alignItems:"center", paddingHorizontal:20, paddingTop:14, paddingBottom:10 }, content:{ padding:20, paddingTop:8, paddingBottom:100 }, brand:{ color:"#2266d5", fontWeight:"800", fontSize:17, letterSpacing:.4 }, title:{ fontSize:32, fontWeight:"800", color:"#172335", marginVertical:14 }, headerTitle:{ fontSize:28, fontWeight:"800", color:"#172335", marginTop:2 }, copy:{ color:"#526174", lineHeight:22, marginBottom:18 }, status:{ color:"#526174", backgroundColor:"#f5f8fb", borderRadius:12, padding:12, marginBottom:16 }, input:{ backgroundColor:"#f5f8fb", padding:15, borderRadius:14, marginBottom:12, color:"#172335", fontSize:16 }, serverHint:{ fontSize:11, color:"#6b7888", lineHeight:16, marginTop:-4, marginBottom:12, paddingHorizontal:4 }, primaryButton:{ backgroundColor:"#2266d5", padding:16, borderRadius:15, alignItems:"center", marginTop:8 }, primaryButtonText:{ color:"white", fontWeight:"700" }, secondaryButton:{ borderWidth:1, borderColor:"#b8c5d4", padding:15, borderRadius:15, alignItems:"center", marginTop:12 }, secondaryButtonText:{ color:"#334258", fontWeight:"700" }, sectionHeader:{ flexDirection:"row", justifyContent:"space-between", alignItems:"center", marginBottom:12 }, sectionTitle:{ fontSize:21, fontWeight:"800", color:"#172335" }, subheading:{ fontSize:18, fontWeight:"800", color:"#172335", marginTop:24, marginBottom:12 }, textButton:{ color:"#2266d5", fontWeight:"700", paddingVertical:6 }, grid:{ flexDirection:"row", flexWrap:"wrap", gap:10 }, assetCard:{ width:"47%", backgroundColor:"#f1f5f9", borderRadius:16, overflow:"hidden", position:"relative", marginBottom:4 }, assetImage:{ width:"100%", height:142 }, image:{ backgroundColor:"#d3dde8" }, placeholder:{ backgroundColor:"#d3dde8", alignItems:"center", justifyContent:"center" }, placeholderText:{ color:"#8ca0b5", fontSize:28 }, assetName:{ color:"#334258", fontSize:12, padding:9 }, videoBadge:{ position:"absolute", top:8, right:8, backgroundColor:"#172335aa", borderRadius:12, paddingHorizontal:7, paddingVertical:3 }, videoBadgeText:{ color:"white", fontSize:11 }, empty:{ color:"#6b7888", paddingVertical:22, textAlign:"center" }, searchRow:{ flexDirection:"row", gap:8, alignItems:"flex-start" }, searchInput:{ flex:1 }, searchButton:{ backgroundColor:"#2266d5", paddingHorizontal:14, paddingVertical:16, borderRadius:14 }, horizontal:{ gap:12 }, albumCard:{ width:150 }, albumImage:{ height:115, width:150, borderRadius:14 }, albumName:{ color:"#172335", fontWeight:"800", marginTop:7 }, albumMeta:{ color:"#6b7888", fontSize:12, marginTop:2 }, place:{ backgroundColor:"#f1f5f9", padding:9, borderRadius:15, flexDirection:"row", alignItems:"center", marginBottom:10, gap:10 }, placeImage:{ width:52, height:52, borderRadius:11 }, placeText:{ flex:1 }, placeName:{ color:"#172335", fontWeight:"700" }, statsRow:{ flexDirection:"row", gap:10, marginBottom:10 }, stat:{ flex:1, backgroundColor:"#f1f5f9", borderRadius:15, padding:13, alignItems:"center" }, statValue:{ fontSize:24, color:"#172335", fontWeight:"800" }, statLabel:{ color:"#6b7888", fontSize:11, textAlign:"center", marginTop:3 }, progress:{ color:"#526174", lineHeight:22, marginTop:18, backgroundColor:"#f5f8fb", borderRadius:12, padding:13 }, setting:{ flexDirection:"row", justifyContent:"space-between", alignItems:"center", paddingVertical:15, borderBottomWidth:1, borderBottomColor:"#d6dee8" }, settingText:{ flex:1, paddingRight:12 }, settingLabel:{ color:"#172335", fontSize:16, fontWeight:"700" }, settingDetail:{ color:"#6b7888", fontSize:12, marginTop:3, lineHeight:17 }, fieldLabel:{ color:"#526174", marginBottom:7, fontSize:13 }, hourRow:{ flexDirection:"row", alignItems:"center", gap:10 }, hourInput:{ flex:1 }, hourSeparator:{ color:"#6b7888", marginBottom:12 }, tabs:{ position:"absolute", bottom:0, left:0, right:0, flexDirection:"row", backgroundColor:"#f5f8fb", borderTopWidth:1, borderTopColor:"#d6dee8", paddingTop:8, paddingBottom:Platform.OS === "ios" ? 22 : 10 }, tab:{ flex:1, alignItems:"center", paddingVertical:6 }, tabText:{ color:"#6b7888", fontSize:12, fontWeight:"700" }, tabTextActive:{ color:"#2266d5" }, viewerSafe:{ flex:1, backgroundColor:"#172335" }, viewerHeader:{ padding:16, flexDirection:"row", alignItems:"center", justifyContent:"space-between" }, viewerClose:{ color:"#8fc0ff", fontWeight:"700" }, viewerTitle:{ color:"white", maxWidth:"70%", fontWeight:"700" }, viewerContent:{ flex:1, justifyContent:"center", alignItems:"center" }, viewerMedia:{ width:"100%", height:"100%", backgroundColor:"#101821" }, viewerLoading:{ width:"100%", height:"100%", alignItems:"center", justifyContent:"center" }, viewerActions:{ padding:16, backgroundColor:"#eef3f8", flexDirection:"row", gap:10 },
});
