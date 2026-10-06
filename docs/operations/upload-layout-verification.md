# Upload and small-screen verification

## Update an existing Windows installation

Use the updated project files on the host PC. Preserve that PC's existing `.env`,
`.drivebound` directory, `docker-compose.user.yml`, and all media folders. Do not
copy credentials/configuration from a different installation or reset volumes.

Run **Drivebound Setup.cmd** again with the existing installation and existing
folder selections. Setup preserves existing keys and rebuilds the application.
**Drivebound Start.cmd** alone starts existing images; it does not rebuild them.
Refresh the browser after the rebuild completes.

## Checks

1. At 320, 390, 768, and 1024 pixels wide, check registration, onboarding,
   profile, and the library. Repeat at 200% browser zoom and a short window.
   Every form action must be reachable by scrolling, without zooming out.
2. Open Storage and Trash. The background must remain sharp. Clicking the
   backdrop or pressing Escape closes the panel; clicking inside does not.
   On a phone the panel may occupy the full width: use its close button.
3. Check that storage actions wrap within the panel and remain readable.
4. Add several disposable photo/video fixtures. The picker minimizes to a
   bottom progress card automatically. Open details, minimize it, and switch
   library views. Transfers must continue; do not navigate away or reload.
5. Confirm all files say saved and appear in the library. Download an original
   and compare its checksum with the source; thumbnail generation is separate.
6. If saving fails, open details and copy the diagnostic text, then select
   Retry after fixing the drive issue. Within the current page, Retry reuses
   the same session and sends only missing bytes (none if already received).

## Staging files from an older failed attempt

Do not delete or manually move `.resumable` files. This update does not
automatically recover old sessions from another browser/page. Retain them
until their corresponding upload session and host error have been diagnosed.
The upload ID and error category in the new error message identify the failed
save without exposing encryption keys or private filenames in diagnostic logs.

## Automated checks

- `npm run test:uploads` in `frontend`: interruption, final-save failure,
  empty finalization retry, lost response, invalid progress, and bounded retries.
- Backend tests include publication-error diagnostics, retained staging bytes,
  and completion from an empty PATCH while preserving file timestamps.
- Browser visual checks remain a separate requirement; a production build
  passing does not establish that every viewport has been inspected.
