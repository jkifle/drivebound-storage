# Mobile release checklist

The app now has internal-development, preview, and production build profiles in
`eas.json`. Create the EAS project and Apple/Google developer credentials once
for the organization, then keep signing credentials in EAS rather than this
repository.

Before TestFlight or Play internal testing:

1. Set the production API URL through the in-app connection flow and verify
   HTTPS, device registration, push-token registration, a complete backup,
   force-close recovery, and restore-to-library on physical devices.
2. In EAS, configure the iOS bundle ID and preserve the Android
   package `com.jkifle.driveboundbackup`, upload notification credentials, and create
   store listing/privacy disclosures.
3. Run `npm run typecheck`, create preview builds for both platforms, then
   promote only the tested production profile. Use TestFlight and Play Internal
   Testing before store submission.

The iOS operating system decides when background processing may run and may not
run it after a user force-quits the app. Android work is persisted through the
native background-task integration; both platforms retain the SQLite transfer
queue and resume the next time the OS permits the app to run.
