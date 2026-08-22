"use client";

import { useEffect, useRef } from "react";

type DeviceRevocationDialogProps = {
  deviceName: string | null;
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
};

const focusableSelector = "button:not([disabled]), [tabindex]:not([tabindex='-1'])";

export function DeviceRevocationDialog({
  deviceName,
  busy,
  error,
  onCancel,
  onConfirm,
}: DeviceRevocationDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const busyRef = useRef(busy);

  useEffect(() => { busyRef.current = busy; }, [busy]);

  useEffect(() => {
    if (!deviceName) return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const frame = window.requestAnimationFrame(() => cancelRef.current?.focus());
    const handleKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape" && !busyRef.current) {
        event.preventDefault();
        onCancel();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(focusableSelector))
        .filter((element) => element.getClientRects().length > 0);
      if (!focusable.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    dialog.addEventListener("keydown", handleKey);
    return () => {
      window.cancelAnimationFrame(frame);
      dialog.removeEventListener("keydown", handleKey);
      document.body.style.overflow = previousOverflow;
      if (previousFocus?.isConnected) previousFocus.focus();
      else document.getElementById("backup-devices-title")?.focus();
    };
  }, [deviceName, onCancel]);

  if (!deviceName) return null;

  return <div
    className="reauth-scrim"
    ref={dialogRef}
    role="dialog"
    aria-modal="true"
    aria-labelledby="device-revoke-title"
    aria-describedby="device-revoke-description device-revoke-consequence"
    aria-busy={busy}
    tabIndex={-1}
  >
    <section className="reauth-card device-revoke-card">
      <div className="reauth-heading">
        <div><span className="eyebrow">Remove device access</span><h2 id="device-revoke-title">Disconnect {deviceName}?</h2></div>
        <button className="icon-button" type="button" onClick={onCancel} disabled={busy} aria-label="Cancel device disconnection">X</button>
      </div>
      <p id="device-revoke-description">Drivebound will immediately invalidate the credential saved by this mobile or backup device.</p>
      <p id="device-revoke-consequence">New backups from this device will stop until it is connected again. Media already stored in your library will remain available.</p>
      <p className="sr-only" role="status" aria-live="polite">{busy ? `Disconnecting ${deviceName}.` : ""}</p>
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="button-row">
        <button ref={cancelRef} className="secondary-button" type="button" onClick={onCancel} disabled={busy}>Keep connected</button>
        <button className="device-revoke-confirm" type="button" onClick={onConfirm} disabled={busy}>{busy ? "Disconnecting..." : "Disconnect device"}</button>
      </div>
    </section>
  </div>;
}
