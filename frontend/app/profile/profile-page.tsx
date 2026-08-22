"use client";

import Link from "../safe-link";
import { FormEvent, KeyboardEvent as ReactKeyboardEvent, useCallback, useEffect, useState } from "react";
import { API_URL, apiRequest } from "../api-client";
import { apiProblemMessage } from "../api-problem";
import type { DriveboundUser } from "../auth-gate";
import { DeviceRevocationDialog } from "../device-revocation-dialog";
import { PasskeyManager } from "../passkey-manager";
import { ReauthenticationDialog, type ReauthenticationCredentials, type ReauthenticationMethod } from "../reauthentication-dialog";
import {
  apiErrorFromResponse,
  beginPasskeyReauthentication,
  beginPasskeyRegistration,
  finishPasskeyReauthentication,
  finishPasskeyRegistration,
  listPasskeys,
  PasskeyApiError,
  type PasskeyRecord,
  recentAuthenticationRequired,
  reauthenticateWithPassword,
  removePasskey,
  renamePasskey,
} from "../passkey-api";
import { createPasskey, describePasskeyError, getPasskey, passkeysSupported, platformAuthenticatorAvailable } from "../webauthn";

type Session = { id: string; ip_address: string | null; user_agent: string | null; last_seen_at: string; expires_at: string; created_at: string; current: boolean };
type BackupDevice = {
  id: string;
  user_id: string;
  name: string;
  platform: string;
  last_seen_at: string | null;
  last_backup_at: string | null;
  files_backed_up: number;
  bytes_backed_up: number;
  created_at: string;
};
type Audit = { id: string; event_type: string; ip_address: string | null; user_agent: string | null; created_at: string };
type ApiResult = { message?: string; detail?: string | { code?: string; message?: string }; recovery_codes?: string[] };
type MessageTone = "status" | "error";
type PendingSecurityAction = { action: string; run: () => Promise<void> };

const googleReauthenticationUrl = `${API_URL}/api/v1/auth/reauthenticate/google/start`;
const googleReauthenticationErrors: Record<string, string> = {
  google_not_linked: "Google is not linked to this account. Use a password or passkey to verify this change.",
  google_reauth_cancelled: "Google verification was cancelled. No account changes were made.",
  google_reauth_expired: "Google verification expired. Start the verification again when you are ready.",
  google_reauth_failed: "Google could not verify this session. Try again or use another verification method.",
};

const profileTabs = ["account", "security", "sessions", "activity", "data"] as const;
type ProfileTab = typeof profileTabs[number];
const profileTabLabels: Record<ProfileTab, string> = { account: "account", security: "security", sessions: "devices", activity: "activity", data: "data" };

const formatBytes = (value: number) => {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / 1024 ** index;
  return `${amount.toLocaleString(undefined, { maximumFractionDigits: index === 0 ? 0 : 1 })} ${units[index]}`;
};

const browserSessionName = (session: Session) => {
  if (session.current) return "This browser";
  return session.user_agent?.split(" ").slice(0, 3).join(" ") || "Unknown browser";
};

export function ProfilePage() {
  const [user, setUser] = useState<DriveboundUser | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [devices, setDevices] = useState<BackupDevice[]>([]);
  const [devicesLoading, setDevicesLoading] = useState(true);
  const [deviceLoadError, setDeviceLoadError] = useState<string | null>(null);
  const [deviceToRevoke, setDeviceToRevoke] = useState<BackupDevice | null>(null);
  const [deviceRevocationError, setDeviceRevocationError] = useState<string | null>(null);
  const [events, setEvents] = useState<Audit[]>([]);
  const [tab, setTab] = useState<ProfileTab>("account");
  const [message, setMessage] = useState<string | null>(null);
  const [messageTone, setMessageTone] = useState<MessageTone>("status");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [passwordChangeCode, setPasswordChangeCode] = useState("");
  const [mfaSecret, setMfaSecret] = useState("");
  const [mfaUri, setMfaUri] = useState("");
  const [mfaCode, setMfaCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [mfaPassword, setMfaPassword] = useState("");
  const [mfaDisableCode, setMfaDisableCode] = useState("");
  const [deletePassword, setDeletePassword] = useState("");
  const [deleteCode, setDeleteCode] = useState("");
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [passkeys, setPasskeys] = useState<PasskeyRecord[]>([]);
  const [passkeySupported, setPasskeySupported] = useState(false);
  const [platformPasskey, setPlatformPasskey] = useState(false);
  const [pendingSecurityAction, setPendingSecurityAction] = useState<PendingSecurityAction | null>(null);
  const [reauthMethod, setReauthMethod] = useState<ReauthenticationMethod>(null);
  const [reauthError, setReauthError] = useState<string | null>(null);

  const announce = (value: string, tone: MessageTone = "status") => {
    setMessage(value);
    setMessageTone(tone);
  };

  const load = useCallback(async () => {
    setLoadError(null);
    setDeviceLoadError(null);
    setDevicesLoading(true);
    try {
      const me = await apiRequest("/api/v1/auth/me");
      if (!me.ok) { window.location.assign("/auth"); return; }
      const account: DriveboundUser = await me.json();
      setUser(account);
      setName(account.display_name ?? "");
      const [sessionList, deviceList, auditList, passkeyList] = await Promise.all([
        apiRequest("/api/v1/auth/sessions"),
        apiRequest("/api/v1/devices"),
        apiRequest("/api/v1/auth/audit"),
        listPasskeys().catch(() => null),
      ]);
      if (sessionList.ok) setSessions(await sessionList.json());
      if (deviceList.ok) setDevices(await deviceList.json());
      else setDeviceLoadError("Mobile and backup devices could not be loaded.");
      if (auditList.ok) setEvents(await auditList.json());
      if (passkeyList) setPasskeys(passkeyList);
      if (!sessionList.ok || !auditList.ok || !passkeyList) setLoadError("Some account security information could not be refreshed.");
    } catch {
      setLoadError("Drivebound could not load your account. Check the connection and try again.");
      setDeviceLoadError("Mobile and backup devices could not be loaded. Check the connection and try again.");
    } finally {
      setDevicesLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const supported = passkeysSupported();
      setPasskeySupported(supported);
      if (supported) void platformAuthenticatorAvailable().then(setPlatformPasskey);
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    const parameters = new URLSearchParams(window.location.search);
    const openSecurity = parameters.get("tab") === "security";
    const googleVerified = parameters.get("reauthenticated") === "google";
    const errorCode = parameters.get("reauth_error");
    const googleError = errorCode ? googleReauthenticationErrors[errorCode] ?? "Google verification could not be completed. Try again or use another method." : null;
    if (!openSecurity && !googleVerified && !errorCode) return;
    window.history.replaceState(window.history.state, "", `${window.location.pathname}${window.location.hash}`);
    const timer = window.setTimeout(() => {
      if (openSecurity || googleVerified || errorCode) setTab("security");
      if (googleError) {
        setMessage(googleError);
        setMessageTone("error");
      } else if (googleVerified) {
        setMessage("Identity verified with Google for this session. Retry the security change you were making.");
        setMessageTone("status");
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  const securityErrorMessage = (reason: unknown) => reason instanceof PasskeyApiError ? reason.message : describePasskeyError(reason);

  const runSecurityAction = async (action: string, operation: () => Promise<void>) => {
    setBusyAction(action);
    setMessage(null);
    try {
      await operation();
    } catch (reason) {
      if (recentAuthenticationRequired(reason)) {
        setPendingSecurityAction({ action, run: operation });
        setReauthError(null);
      } else announce(securityErrorMessage(reason), "error");
    } finally {
      setBusyAction(null);
    }
  };

  const closeReauthentication = useCallback(() => {
    setPendingSecurityAction(null);
    setReauthError(null);
    setReauthMethod(null);
  }, []);

  const resumeSecurityAction = async (pending: PendingSecurityAction) => {
    setPendingSecurityAction(null);
    setReauthError(null);
    announce("Identity verified. Continuing your security change.");
    await runSecurityAction(pending.action, pending.run);
  };

  const verifyPasswordForReauthentication = async ({ password, code }: ReauthenticationCredentials) => {
    const pending = pendingSecurityAction;
    if (!pending) return;
    setReauthMethod("password");
    setReauthError(null);
    try {
      await reauthenticateWithPassword(password, code);
      await resumeSecurityAction(pending);
    } catch (reason) {
      setReauthError(securityErrorMessage(reason));
    } finally {
      setReauthMethod(null);
    }
  };

  const verifyPasskeyForReauthentication = async () => {
    const pending = pendingSecurityAction;
    if (!pending) return;
    setReauthMethod("passkey");
    setReauthError(null);
    try {
      const options = await beginPasskeyReauthentication();
      const credential = await getPasskey(options.public_key);
      await finishPasskeyReauthentication(options.challenge_id, credential);
      await resumeSecurityAction(pending);
    } catch (reason) {
      setReauthError(securityErrorMessage(reason));
    } finally {
      setReauthMethod(null);
    }
  };

  const verifyGoogleForReauthentication = () => {
    if (!user?.reauth_methods.includes("google")) {
      setReauthError("Google is not available as a verification method for this account.");
      return;
    }
    setReauthMethod("google");
    setReauthError(null);
    window.location.assign(googleReauthenticationUrl);
  };

  const jsonAction = async (action: string, path: string, body: object, method = "POST") => {
    setBusyAction(action);
    setMessage(null);
    try {
      const response = await apiRequest(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const result: ApiResult = await response.json().catch(() => ({}));
      announce(apiProblemMessage(result, response.ok ? "Saved." : "The change could not be saved."), response.ok ? "status" : "error");
      if (response.ok) await load();
      return { ok: response.ok, result };
    } catch {
      announce("Drivebound could not complete that request. Check the connection and try again.", "error");
      return { ok: false, result: {} as ApiResult };
    } finally {
      setBusyAction(null);
    }
  };

  const updateProfile = (event: FormEvent) => {
    event.preventDefault();
    void jsonAction("profile", "/api/v1/auth/profile", { display_name: name }, "PATCH");
  };

  const changePassword = (event: FormEvent) => {
    event.preventDefault();
    if (!user) return;
    void runSecurityAction("password", async () => {
      const response = await apiRequest("/api/v1/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: user.has_password ? currentPassword : undefined,
          new_password: newPassword,
          otp: user.has_password && user.two_factor_enabled ? passwordChangeCode : undefined,
        }),
      });
      if (!response.ok) throw await apiErrorFromResponse(response, user.has_password ? "Password could not be changed." : "Password could not be set.");
      const result: ApiResult = await response.json();
      announce(apiProblemMessage(result, user.has_password ? "Password changed." : "Password added to your account."));
      setCurrentPassword("");
      setNewPassword("");
      setPasswordChangeCode("");
      await load();
    });
  };

  const startMfa = async () => {
    await runSecurityAction("mfa-setup", async () => {
      const response = await apiRequest("/api/v1/auth/mfa/setup", { method: "POST" });
      if (!response.ok) throw await apiErrorFromResponse(response, "Two-factor setup could not be started.");
      const value: { secret: string; provisioning_uri: string } = await response.json();
      setMfaSecret(value.secret);
      setMfaUri(value.provisioning_uri);
      announce("Add the secret to your authenticator, then enter its six-digit code.");
    });
  };

  const confirmMfa = async () => {
    await runSecurityAction("mfa-confirm", async () => {
      const response = await apiRequest("/api/v1/auth/mfa/confirm", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: mfaCode }),
      });
      if (!response.ok) throw await apiErrorFromResponse(response, "Two-factor setup could not be confirmed.");
      const result: ApiResult = await response.json();
      setRecoveryCodes(result.recovery_codes ?? []);
      announce("Two-factor authentication is enabled. Save the recovery codes now.");
      await load();
    });
  };

  const disableMfa = async () => {
    if (!user) return;
    await runSecurityAction("mfa-disable", async () => {
      const response = await apiRequest("/api/v1/auth/mfa", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          password: user.has_password ? mfaPassword : undefined,
          code: user.has_password ? mfaDisableCode : undefined,
        }),
      });
      if (!response.ok) throw await apiErrorFromResponse(response, "Two-factor authentication could not be disabled.");
      const result: ApiResult = await response.json();
      announce(apiProblemMessage(result, "Two-factor authentication disabled."));
      setMfaPassword("");
      setMfaDisableCode("");
      setRecoveryCodes([]);
      setMfaSecret("");
      setMfaCode("");
      await load();
    });
  };

  const exportAccount = async () => {
    await runSecurityAction("export", async () => {
      const response = await apiRequest("/api/v1/auth/export");
      if (!response.ok) throw await apiErrorFromResponse(response, "Your account export could not be prepared.");
      const blob = new Blob([JSON.stringify(await response.json(), null, 2)], { type: "application/json" });
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = "drivebound-account-export.json";
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
      announce("Account export downloaded.");
    });
  };

  const deleteAccount = async (event: FormEvent) => {
    event.preventDefault();
    if (!user) return;
    await runSecurityAction("delete", async () => {
      const response = await apiRequest("/api/v1/auth/account", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          password: user.has_password ? deletePassword : undefined,
          code: user.has_password && user.two_factor_enabled ? deleteCode || undefined : undefined,
          confirmation: deleteConfirmation,
        }),
      });
      if (!response.ok) throw await apiErrorFromResponse(response, "Your account could not be deleted.");
      window.location.assign("/");
    });
  };

  const signOut = async () => {
    setBusyAction("signout");
    try {
      const response = await apiRequest("/api/v1/auth/logout", { method: "POST" });
      if (!response.ok) { announce("Sign out could not be completed. Please try again.", "error"); return; }
      window.location.assign("/");
    } catch {
      announce("Sign out could not reach Drivebound.", "error");
    } finally {
      setBusyAction(null);
    }
  };

  const revokeSession = async (item: Session) => {
    await runSecurityAction(`session-${item.id}`, async () => {
      const response = await apiRequest(`/api/v1/auth/sessions/${item.id}`, { method: "DELETE" });
      if (!response.ok) throw await apiErrorFromResponse(response, "That session could not be revoked.");
      announce("Session revoked.");
      await load();
    });
  };

  const retryDevices = async () => {
    setDevicesLoading(true);
    setDeviceLoadError(null);
    try {
      const response = await apiRequest("/api/v1/devices");
      if (!response.ok) throw await apiErrorFromResponse(response, "Mobile and backup devices could not be loaded.");
      setDevices(await response.json());
    } catch (reason) {
      setDeviceLoadError(reason instanceof PasskeyApiError ? reason.message : "Mobile and backup devices could not be loaded. Check the connection and try again.");
    } finally {
      setDevicesLoading(false);
    }
  };

  const requestDeviceRevocation = (device: BackupDevice) => {
    setDeviceRevocationError(null);
    setDeviceToRevoke(device);
  };

  const cancelDeviceRevocation = useCallback(() => {
    setDeviceRevocationError(null);
    setDeviceToRevoke(null);
  }, []);

  const confirmDeviceRevocation = async () => {
    const device = deviceToRevoke;
    if (!device) return;
    const action = `device-${device.id}`;
    setBusyAction(action);
    setDeviceRevocationError(null);
    try {
      const response = await apiRequest(`/api/v1/devices/${device.id}`, { method: "DELETE" });
      if (!response.ok) throw await apiErrorFromResponse(response, "That device could not be disconnected.");
      setDevices((current) => current.filter((item) => item.id !== device.id));
      setDeviceToRevoke(null);
      announce(`${device.name} disconnected. Its saved device credential can no longer access this account.`);
    } catch (reason) {
      setDeviceRevocationError(reason instanceof PasskeyApiError ? reason.message : "That device could not be disconnected. Check the connection and try again.");
    } finally {
      setBusyAction(null);
    }
  };

  const enrollPasskey = async () => {
    if (!passkeySupported) {
      announce("Passkeys require a supported browser and a secure Drivebound address.", "error");
      return;
    }
    await runSecurityAction("passkey-enroll", async () => {
      const options = await beginPasskeyRegistration();
      const credential = await createPasskey(options.public_key);
      const registered = await finishPasskeyRegistration(options.challenge_id, credential, platformPasskey ? "This device" : "Security key");
      setPasskeys((current) => [registered, ...current.filter((item) => item.id !== registered.id)]);
      announce(`Passkey "${registered.name}" added.`);
      await load();
    });
  };

  const updatePasskeyName = async (credentialId: string, label: string) => {
    await runSecurityAction(`passkey-rename-${credentialId}`, async () => {
      const updated = await renamePasskey(credentialId, label);
      setPasskeys((current) => current.map((item) => item.id === updated.id ? updated : item));
      announce(`Passkey renamed to "${updated.name}".`);
    });
  };

  const deletePasskey = async (credentialId: string) => {
    await runSecurityAction(`passkey-remove-${credentialId}`, async () => {
      await removePasskey(credentialId);
      setPasskeys((current) => current.filter((item) => item.id !== credentialId));
      announce("Passkey removed.");
      await load();
    });
  };

  const selectTab = (next: ProfileTab) => {
    setTab(next);
    setMessage(null);
  };

  const handleTabKey = (event: ReactKeyboardEvent<HTMLButtonElement>, current: ProfileTab) => {
    const index = profileTabs.indexOf(current);
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex = (index + 1) % profileTabs.length;
    if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextIndex = (index - 1 + profileTabs.length) % profileTabs.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = profileTabs.length - 1;
    if (nextIndex == null) return;
    event.preventDefault();
    const next = profileTabs[nextIndex];
    selectTab(next);
    document.getElementById(`profile-tab-${next}`)?.focus();
  };

  if (!user) return <main className="app-loading" id="main-content" aria-busy={!loadError}>{loadError ? <section className="load-error" role="alert"><h1>Profile unavailable</h1><p>{loadError}</p><button className="secondary-button" onClick={() => void load()}>Try again</button></section> : <p role="status">Opening your profile...</p>}</main>;

  return <main className="profile-shell" id="main-content">
    <header className="profile-header">
      <Link className="brand" href="/"><span className="brand-mark" aria-hidden="true">D</span>Drivebound</Link>
      <nav aria-label="Account navigation"><Link href="/library">Library</Link><button className="secondary-button" disabled={busyAction === "signout"} onClick={() => void signOut()}>{busyAction === "signout" ? "Signing out..." : "Sign out"}</button></nav>
    </header>
    <div className="profile-layout">
      <aside>
        <div className="profile-avatar" aria-hidden="true">{(user.display_name ?? user.email)[0].toUpperCase()}</div>
        <h1>{user.display_name ?? "Drivebound account"}</h1><p>{user.email}</p>
        <div className="profile-tablist" role="tablist" aria-label="Profile settings">{profileTabs.map((item) => <button key={item} id={`profile-tab-${item}`} role="tab" aria-selected={tab === item} aria-controls="profile-tabpanel" tabIndex={tab === item ? 0 : -1} className={tab === item ? "active" : ""} onClick={() => selectTab(item)} onKeyDown={(event) => handleTabKey(event, item)}>{profileTabLabels[item]}</button>)}</div>
      </aside>
      <section className="profile-panel" id="profile-tabpanel" role="tabpanel" aria-labelledby={`profile-tab-${tab}`} tabIndex={-1} aria-busy={busyAction != null}>
        {loadError && <p className="profile-message error" role="alert">{loadError}</p>}
        {message && <p className={`profile-message ${messageTone === "error" ? "error" : ""}`} role={messageTone === "error" ? "alert" : "status"}>{message}</p>}
        {tab === "account" && <><span className="eyebrow">Profile</span><h2>Account information</h2><div className="status-card"><span className={`drive-light ${user.email_verified_at ? "online" : "unavailable"}`} aria-hidden="true" /><div><strong>{user.email_verified_at ? "Email verified" : "Email verification pending"}</strong><small>{user.email_verified_at ? new Date(user.email_verified_at).toLocaleString() : "Check your inbox for a verification link."}</small></div></div><form className="settings-form" onSubmit={updateProfile}><label>Display name<input value={name} onChange={(event) => setName(event.target.value)} autoComplete="name" /></label><label>Email address<input value={user.email} disabled /></label><button className="primary-button" disabled={busyAction != null}>{busyAction === "profile" ? "Saving..." : "Save profile"}</button></form></>}
        {tab === "security" && <><span className="eyebrow">Security</span><h2>Password, passkeys, and two-factor authentication</h2><form className="settings-form" onSubmit={changePassword}><h3>{user.has_password ? "Change password" : "Add a password"}</h3><p>{user.has_password ? "Confirm your current credentials before replacing your password." : "Create a password as another sign-in and recovery method. Drivebound will ask you to verify with Google or a passkey first."}</p>{user.has_password && <label>Current password<input type="password" required value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} autoComplete="current-password" /></label>}{user.has_password && user.two_factor_enabled && <label>Authenticator or recovery code<input required value={passwordChangeCode} onChange={(event) => setPasswordChangeCode(event.target.value)} autoComplete="one-time-code" /></label>}<label>{user.has_password ? "New password" : "Create password"}<input type="password" minLength={12} required value={newPassword} onChange={(event) => setNewPassword(event.target.value)} autoComplete="new-password" /></label><button className="primary-button" disabled={busyAction != null || !newPassword || (user.has_password && (!currentPassword || (user.two_factor_enabled && !passwordChangeCode.trim())))}>{busyAction === "password" ? user.has_password ? "Changing..." : "Adding..." : user.has_password ? "Change password" : "Add password"}</button></form><PasskeyManager credentials={passkeys.map((item) => ({ id: item.id, label: item.name, createdAt: item.created_at, lastUsedAt: item.last_used_at }))} supported={passkeySupported} platformAvailable={platformPasskey} busyAction={busyAction} onEnroll={() => void enrollPasskey()} onRename={(credentialId, label) => void updatePasskeyName(credentialId, label)} onRemove={(credentialId) => void deletePasskey(credentialId)} /><div className="settings-card"><h3>Authenticator app</h3><p>{user.two_factor_enabled ? "Two-factor authentication is protecting this account." : "Add a time-based code from any compatible authenticator app."}</p>{user.two_factor_enabled && <div className="mfa-setup">{user.has_password && <label>Password<input type="password" value={mfaPassword} onChange={(event) => setMfaPassword(event.target.value)} autoComplete="current-password" /></label>}{user.has_password && <label>Authenticator or recovery code<input value={mfaDisableCode} onChange={(event) => setMfaDisableCode(event.target.value)} autoComplete="one-time-code" /></label>}{!user.has_password && <p className="security-note">Fresh passkey verification, or Google verification plus your local authenticator or recovery factor, completes this proof. No second code is required here.</p>}<button type="button" className="secondary-button" disabled={busyAction != null || (user.has_password && (!mfaDisableCode || !mfaPassword))} onClick={() => void disableMfa()}>{busyAction === "mfa-disable" ? "Disabling..." : "Disable two-factor authentication"}</button></div>}{!user.two_factor_enabled && !mfaSecret && <button type="button" className="secondary-button" disabled={busyAction != null} onClick={() => void startMfa()}>{busyAction === "mfa-setup" ? "Starting setup..." : "Set up two-factor authentication"}</button>}{mfaSecret && <div className="mfa-setup"><label>Setup secret<input readOnly value={mfaSecret} onFocus={(event) => event.currentTarget.select()} /></label><details><summary>Authenticator URI</summary><code>{mfaUri}</code></details><label>Six-digit code<input inputMode="numeric" pattern="[0-9]*" value={mfaCode} onChange={(event) => setMfaCode(event.target.value)} autoComplete="one-time-code" /></label><button type="button" className="primary-button" disabled={busyAction != null || !mfaCode.trim()} onClick={() => void confirmMfa()}>{busyAction === "mfa-confirm" ? "Confirming..." : "Confirm and enable"}</button></div>}{recoveryCodes.length > 0 && <div className="recovery-codes" role="status"><strong>Save these one-time recovery codes now</strong>{recoveryCodes.map((code) => <code key={code}>{code}</code>)}</div>}</div></>}
        {tab === "sessions" && <>
          <span className="eyebrow">Account access</span>
          <h2>Browsers and backup devices</h2>
          <p>Browser sessions sign you into the web library. Mobile and backup devices use a separate, durable credential for automatic uploads.</p>
          <section className="access-section" aria-labelledby="browser-sessions-title">
            <h3 id="browser-sessions-title">Browser sessions</h3>
            <p>Revoke a browser session you no longer recognize. Your current browser remains available here so you do not sign yourself out by mistake.</p>
            <div className="session-list">
              {sessions.map((item) => <article key={item.id}>
                <div>
                  <strong>{browserSessionName(item)}</strong>
                  <span>{item.ip_address ?? "Unknown network"} - Last active <time dateTime={item.last_seen_at}>{new Date(item.last_seen_at).toLocaleString()}</time></span>
                </div>
                {!item.current && <button aria-label={`Revoke browser session ${browserSessionName(item)}`} disabled={busyAction != null} onClick={() => void revokeSession(item)}>{busyAction === `session-${item.id}` ? "Revoking..." : "Revoke session"}</button>}
              </article>)}
              {sessions.length === 0 && <p className="settings-empty">No active browser sessions were returned.</p>}
            </div>
          </section>
          <section className="access-section" aria-labelledby="backup-devices-title">
            <h3 id="backup-devices-title" tabIndex={-1}>Mobile and backup devices</h3>
            <p>Disconnecting a device invalidates its saved credential and stops new backups. It does not delete media already stored in Drivebound.</p>
            <div className="device-access-list" aria-busy={devicesLoading}>
              {devicesLoading && <p className="settings-empty" role="status">Loading connected devices...</p>}
              {!devicesLoading && deviceLoadError && <div className="device-list-error"><p role="alert">{deviceLoadError}</p><button className="secondary-button" type="button" onClick={() => void retryDevices()}>Try again</button></div>}
              {!devicesLoading && !deviceLoadError && devices.map((device) => <article key={device.id}>
                <div id={`device-summary-${device.id}`}>
                  <strong>{device.name}</strong>
                  <span>{device.platform} - Connected <time dateTime={device.created_at}>{new Date(device.created_at).toLocaleDateString()}</time></span>
                  <span>{device.last_seen_at ? <>Last contact <time dateTime={device.last_seen_at}>{new Date(device.last_seen_at).toLocaleString()}</time></> : "No contact reported yet"}</span>
                  <span>{device.last_backup_at ? <>Last backup <time dateTime={device.last_backup_at}>{new Date(device.last_backup_at).toLocaleString()}</time></> : "No completed backup reported"} - {device.files_backed_up.toLocaleString()} files - {formatBytes(device.bytes_backed_up)}</span>
                </div>
                <button
                  type="button"
                  aria-label={`Disconnect ${device.name}`}
                  aria-describedby={`device-summary-${device.id}`}
                  disabled={busyAction != null}
                  onClick={() => requestDeviceRevocation(device)}
                >Disconnect</button>
              </article>)}
              {!devicesLoading && !deviceLoadError && devices.length === 0 && <p className="settings-empty">No mobile or backup devices are connected.</p>}
            </div>
          </section>
        </>}
        {tab === "activity" && <><span className="eyebrow">Audit trail</span><h2>Security history</h2><div className="session-list">{events.map((item) => <article key={item.id}><div><strong>{item.event_type.replaceAll("_", " ")}</strong><span>{new Date(item.created_at).toLocaleString()} - {item.ip_address ?? "Unknown network"}</span></div></article>)}{events.length === 0 && <p className="settings-empty">No security events were returned.</p>}</div></>}
        {tab === "data" && <><span className="eyebrow">Data control</span><h2>Export or delete your account</h2><div className="settings-card"><h3>Export account manifest</h3><p>Download account information and a list of every asset with its retrieval URL.</p><button className="secondary-button" disabled={busyAction != null} onClick={() => void exportAccount()}>{busyAction === "export" ? "Preparing export..." : "Download export"}</button></div><form className="settings-card danger-zone" onSubmit={deleteAccount}><h3>Delete account</h3><p>This permanently deletes your account and encryption key. Any retained orphaned bytes become inaccessible immediately and must be removed by the administrator during storage cleanup. This cannot be undone.</p>{user.has_password && <label>Password<input type="password" required value={deletePassword} onChange={(event) => setDeletePassword(event.target.value)} autoComplete="current-password" /></label>}{user.has_password && user.two_factor_enabled && <label>Authenticator or recovery code<input required value={deleteCode} onChange={(event) => setDeleteCode(event.target.value)} autoComplete="one-time-code" /></label>}{!user.has_password && <p className="security-note">Fresh passkey verification, or Google verification plus your local authenticator or recovery factor, completes the deletion proof. No second code is required here.</p>}<label>Type DELETE MY ACCOUNT<input required value={deleteConfirmation} onChange={(event) => setDeleteConfirmation(event.target.value)} autoComplete="off" /></label><button disabled={busyAction != null || deleteConfirmation !== "DELETE MY ACCOUNT" || (user.has_password && !deletePassword) || (user.has_password && user.two_factor_enabled && !deleteCode.trim())}>{busyAction === "delete" ? "Deleting account..." : "Delete my account"}</button></form></>}
      </section>
    </div>
    <DeviceRevocationDialog
      deviceName={deviceToRevoke?.name ?? null}
      busy={deviceToRevoke != null && busyAction === `device-${deviceToRevoke.id}`}
      error={deviceRevocationError}
      onCancel={cancelDeviceRevocation}
      onConfirm={() => void confirmDeviceRevocation()}
    />
    <ReauthenticationDialog
      open={pendingSecurityAction != null}
      busyMethod={reauthMethod}
      error={reauthError}
      twoFactorEnabled={user.two_factor_enabled}
      passwordAvailable={user.reauth_methods.includes("password")}
      passkeyAvailable={passkeySupported && user.reauth_methods.includes("passkey")}
      googleAvailable={user.reauth_methods.includes("google")}
      onCancel={closeReauthentication}
      onPassword={(credentials) => void verifyPasswordForReauthentication(credentials)}
      onPasskey={() => void verifyPasskeyForReauthentication()}
      onGoogle={verifyGoogleForReauthentication}
    />
  </main>;
}
