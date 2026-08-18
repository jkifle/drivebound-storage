# Mobile release checklist

The app now has internal-development, preview, and production build profiles in
`eas.json`. Create the EAS project and Apple/Google developer credentials once
for the organization, then keep signing credentials in EAS rather than this
repository.

Before TestFlight or Play internal testing:

1. Configure `EXPO_PUBLIC_API_URL` in the EAS `preview` and `production`
   environments. It is public bundle configuration and must be an HTTPS URL,
   never a token or password. `app.config.ts` rejects a non-HTTPS production
   value; production otherwise starts with a blank server field.
2. Keep the stable iOS bundle ID and Android application ID
   `com.jkifle.driveboundbackup`. EAS owns remote build numbers; initialize them
   once with `eas build:version:set` and never reset a store version.
3. Store Apple/Google signing and push-notification credentials in EAS, not in
   source control or `EXPO_PUBLIC_` variables. The CLI requires a clean commit
   before remote builds so the artifact can be traced to reviewed source.
4. Run `npm run typecheck` and `npx expo config --type public`, create preview
   builds for both platforms, and exercise HTTPS connection validation, device
   registration, notification registration, a complete backup, pause/retry,
   process-restart recovery, paginated browsing, sharing, and restore-to-library
   on physical devices.
5. Promote only that tested source revision with `eas build --profile
   production --platform all`. Use TestFlight and Play Internal Testing before
   store submission; submission remains a separate, reviewed action.

## Store privacy and permission review

- Apple photo-library read access is used to select originals for backup; add
  access is used only after the user chooses to restore an original. Background
  processing and remote notifications support queued backups and status alerts.
- Android requests images/videos, selected-media compatibility, embedded media
  location, and notifications. Audio access is blocked, cleartext networking is
  disabled in release builds, and the debug-only manifest contains development
  permissions.
- Disclose account identifiers, filenames/media, capture timestamps, and
  embedded GPS/camera metadata because users choose to store them on their own
  Drivebound server. Do not claim that GPS is stripped. Drivebound should be
  declared as neither selling data nor using it for cross-app tracking.
- Supply a reachable privacy-policy URL, account-deletion instructions, support
  contact, backup/restore limitations, and screenshots from the exact release
  candidate. Re-check Apple privacy nutrition labels and Google Play Data safety
  answers whenever telemetry, hosted relays, or third-party SDKs change.

The iOS operating system decides when background processing may run and may not
run it after a user force-quits the app. Android work is persisted through the
native background-task integration; both platforms retain the SQLite transfer
queue and resume the next time the OS permits the app to run.
