# Test Drivebound from a laptop or phone

Your stationary PC is the server: it runs Drivebound and keeps the photos on
its connected drive. Your laptop or phone opens Drivebound over a private
internet connection. You do not need Windows Remote Desktop to view, upload,
or download photos. You also do not need Docker or a copy of the repository on
the device used for viewing.

This guide describes a test to perform. It does not mean the real PC, email
provider, remote connection, or storage-recovery checks have already passed.
Start with copies of a few disposable photos and a short video.

## 1. Prepare the stationary PC

Use a supported Windows PC with working internet, power, and a connected photo
drive. Keep the PC awake while plugged in; the screen can turn off. Keep its
drive letters and selected folders stable after setup.

Install [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/),
open it, and finish its installation prompts. Drivebound uses Linux containers.
Docker's Windows requirements include hardware virtualization and a supported
backend such as WSL 2. Resolve any Docker startup or virtualization error before
starting Drivebound setup.

In Docker Desktop's General settings, enable **Start Docker Desktop when you
sign in to your computer**. This is a sign-in setting; someone must sign in to
the installing Windows account after a reboot for this pilot's startup journey.
[Docker's settings guide](https://docs.docker.com/desktop/settings-and-maintenance/settings/).

Get the updated Drivebound project using GitHub Desktop or an extracted ZIP.
Use the version containing all five launchers:

- **Drivebound Setup**
- **Drivebound Start**
- **Drivebound Stop**
- **Drivebound Status**
- **Drivebound Remote Access**

Windows may hide their `.cmd` extensions. A repository clone only contains
changes that have been published to that repository; if the launchers are
missing, obtain the updated project first. Keep using the same project folder
and Windows account once the installation is configured.

For a fresh pilot, let Setup create its configuration. If this PC already hosts
Drivebound accounts or encrypted photos, keep its existing configuration and
storage together; do not replace its `.env` with the example file.

## 2. Choose the storage and create your account

1. Double-click **Drivebound Setup** on the stationary PC.
2. Enter the email address that will own this PC's photo collection. Use that
   same address when registering in the browser later.
3. Choose the existing-photo folder containing your disposable test collection.
   This folder is indexed read-only; the originals stay there.
4. Choose the folder for new uploads. Setup creates and manages its storage
   subfolders for you.
5. If available, choose a protection folder on a different physical disk. That
   disk must differ from both the existing-photo disk and the new-upload disk
   for full independent protection. Different folders or partitions alone do
   not qualify. With one disk you can still test browsing and uploading, but
   you cannot pass the disk-failure protection test.
6. Accept automatic startup after Windows sign-in if you want it. Wait for
   Drivebound to report readiness and open the browser. The initial address is
   normally `http://localhost:3000` on this PC.
7. Create an account with the approved owner email and a password. In the fresh
   local test configuration, registration displays **Open development email
   link**. Open it, verify the account, then return to sign in. If this
   installation already uses real email delivery, use the email you receive
   instead.
8. Complete onboarding: enter your display name, name the collection, and
   confirm the selected folder. You should not need to type a server or
   container path. Wait for the scan, or continue into the library while it runs.

Setup generates the database password and encryption keys automatically. No
key-generation commands or copying of secret values is required. Use local
folders on drives that support Windows file permissions, such as NTFS. Do not
reformat a disk containing photos for this test. More details are in the
[Windows setup guide](windows-setup.md).

Before moving on, open several photos, play the video, and upload one disposable
image through the browser. Check that it appears in the library. Open **Storage**
and review protection status. Select **Back up and verify now** and wait for
the database and configuration backup results.

## 3. Set up the private connection

Install [Tailscale](https://tailscale.com/download) on the stationary PC and on
the external laptop or phone. Sign them into the same intended private Tailscale
network. Tailscale provides the network connection; your Drivebound account
still controls access to your photo library.

In the Tailscale admin console, open **DNS**, enable **MagicDNS**, and enable
**HTTPS Certificates**. Follow its confirmation prompts. Choose a generic PC
name such as `drivebound-home`: the certificate hostname can appear in public
certificate records, while access to the service remains controlled by
Tailscale. [Tailscale's HTTPS instructions](https://tailscale.com/docs/how-to/set-up-https-certificates).

The Drivebound wizard uses Tailscale Serve to make the app reachable by permitted
devices in your private network. Access rules apply to Serve too. This guide
does not require router port forwarding or public Tailscale Funnel.
[How Tailscale Serve works](https://tailscale.com/docs/features/tailscale-serve).

## 4. Enable remote access on the stationary PC

Prepare your email provider's SMTP settings. These are the details Drivebound
needs to send verification and password-recovery messages:

| Wizard field | What to enter |
| --- | --- |
| SMTP server | Your provider's outgoing-mail server name |
| STARTTLS port | The provider's STARTTLS port, commonly 587 |
| SMTP username | The username supplied by the provider, often an email address |
| SMTP app password | The provider-issued app password or SMTP credential |
| Sender email | An address the provider allows you to send from |

Get these values from your email provider. The app password is an email-service
credential, separate from your Drivebound login password and the automatically
generated storage keys. A provider offering only another authentication method
needs a compatible mail setup before this wizard can complete.

1. Confirm Tailscale is connected on the stationary PC.
2. Double-click **Drivebound Remote Access**.
3. Enter the mail settings and choose **Test email and enable**. The wizard
   sends one test message to the approved owner. Check that it arrives,
   including the spam folder.
4. Let the wizard rebuild the browser configuration and check the running
   services. It then enables the private HTTPS routes.
5. Save the exact address it displays, for example
   `https://drivebound-home.example-network.ts.net`. Use your actual address;
   the example here will not connect to your PC.

Real SMTP delivery is required for this remote mode. The local development
verification link is not a substitute. If the wizard reports an error, follow
its recovery message before trying again; do not change security flags or
replace encryption keys to bypass the error.

Use the new HTTPS address on the stationary PC too. Existing localhost passkeys
need to be enrolled again for the new address. Confirm password sign-in works
before switching. Google sign-in is optional and needs separate Google OAuth
configuration; changing the address also requires updating its authorized
callback, as described in the [Windows guide](windows-setup.md).

## 5. Open it from the external device

| Device or screen | Address to use |
| --- | --- |
| Laptop browser | `https://YOUR-PC.YOUR-NETWORK.ts.net` |
| Phone browser | The same address as the laptop browser |
| Native Drivebound app's Server URL field | `https://YOUR-PC.YOUR-NETWORK.ts.net:8443` |

The website uses HTTPS port 443, which the browser does not need to show. The
native app connects to the API on port 8443. Use the complete hostname from the
wizard, not a bare computer name, `localhost`, a drive path, or a Docker address.

For the quickest test, use the laptop or phone browser:

1. Connect Tailscale on the external device.
2. Open the website address from the wizard.
3. Sign in with the same verified Drivebound account you created on the PC.
4. Open the library. Your indexed test photos should appear without copying
   the repository or mounting the PC's disk on this device.

The native app is a separate test. You need an installed Drivebound native
test/release build; cloning the repository does not install it on a phone, and
this guide does not promise an app-store release. Start with the browser if no
build has been supplied. Developer build instructions are in the
[mobile README](../../mobile/README.md) and
[release checklist](../../mobile/RELEASE.md).

In an installed native build, enter the API address ending in `:8443`, your
email and password, and the authenticator code if enabled, then tap **Connect
device**. The current native connection screen uses password sign-in. If your
web account uses only Google/passkeys, add a password through your web profile
first. Grant only the media permissions appropriate for this disposable test.

For a native upload test, open **Backup** and select **Back up now**. This can
scan the photo-library items the app is allowed to access, not just one selected
file. Use a test device or limited-photo permission where available. **Wi-Fi
only** is on by default; a cellular backup needs it temporarily turned off in
**Settings**. Manual backup still respects pause and charging restrictions.
Restore your preferred settings afterward. Browser uploads do not use this
native backup policy, and a phone browser does not provide automatic
camera-roll backup.

## 6. Prove the connection works away from home

On a phone, turn off Wi-Fi and keep Tailscale connected. On a laptop, use a
different internet connection, such as a phone hotspot, with Tailscale running
on the laptop itself. Loading a previously cached page is not enough.

1. Sign out and sign back in through the HTTPS website.
2. Open several photos and play the short video.
3. Download an original and open it. Check its filename and recorded date,
   location, and camera details where the source contains them. The download
   folder may assign a new filesystem timestamp; that is separate from the
   media's embedded metadata.
4. Upload a new disposable photo. Refresh Drivebound on the stationary PC and
   confirm it appears there too.
5. Refresh the external device and confirm the upload remains available.
6. Check **Storage** for its processing/protection result. An upload finishing
   does not by itself prove a verified copy exists on a second disk.
7. Exercise **Forgot password** with the disposable account, confirm the actual
   email arrives, and use the new password to sign back in. Keep Tailscale
   connected when opening the recovery link.

Check both access boundaries. With Tailscale disconnected on an external
device, a fresh request should not retrieve new private media. With Tailscale
connected, use a private browser window to register a second test account with
another email. That account must not see the owner's library or gain the
ability to connect the owner's host photo folder. It may use its own uploads.

## 7. Check restart, storage, and stopping

After the basic remote test works, reboot the stationary PC and sign back in
to the same Windows account. Wait for Docker and Drivebound to recover, then
make a fresh remote request. Record how long recovery takes; the pilot target
is five minutes after Windows sign-in. Before-sign-in operation is not part of
this Windows setup.

For a safe first storage check, finish all test transfers, double-click
**Drivebound Stop**, and eject a test disk normally. **Drivebound Status** should
report that the selected storage is unavailable. Reconnect that same disk and
use **Drivebound Start**. The same account and photos should remain available;
Setup should not generate a new configuration or adopt a different disk.

This stopped-service check does not prove recovery from a disk disappearing
during operation. Use the disposable-only interruption and protection-copy
restore procedures in the [pilot checklist](stationary-pc-pilot.md) for that
acceptance gate. Do not pull a working disk containing real data to test a
failure. A complete database/media disaster restore is a separate
[recovery drill](recovery-drill.md).

To stop hosting, double-click **Drivebound Stop** on the PC. Photos and accounts
remain stored, and the saved stop choice prevents the sign-in task from
restarting the application until you choose **Drivebound Start**. Tailscale may
remain connected, but the stopped Drivebound service will be unavailable.

## If a step fails

| What you see | Next action |
| --- | --- |
| Docker is stopped, crashes, or reports virtualization missing | Open Docker Desktop and resolve its startup message first. For the Inference manager error, follow the targeted section in the Windows guide; do not reset Docker or replace Drivebound's keys. |
| Setup reports the application is not ready | Double-click **Drivebound Status**. Reconnect the approved drives, review Docker's service state, then use **Drivebound Start**. |
| The website works on the PC but not externally | Check that the PC is awake, both devices are connected to the intended Tailscale network, and the external device uses the full HTTPS address. Review any Tailscale access rules for both 443 and 8443. |
| The page loads but sign-in or photos fail | Verify that Remote Access completed successfully and the API route on 8443 is allowed. Use the website address without 8443 in the browser; use 8443 only for the native app's server setting. |
| An email does not arrive | Check spam, the permitted sender, SMTP server/STARTTLS port, and the provider's app credential. Do not assume a successful local development link proves mail delivery. |
| Your account cannot connect the selected folder | Register/sign in with the exact owner email approved in Setup and complete verification. Another account is intentionally not granted the host folder. |
| A native backup waits on cellular | Check **Settings → Wi-Fi only**, pause, charging restrictions, media permission, and Tailscale connectivity. |
| A drive identity is missing or changed | Reconnect the original drive. Do not recreate empty storage folders, remove identity markers, or generate new keys. |
| Tailscale reports a conflicting service or incomplete route cleanup | Preserve its existing services and follow the wizard's message. If it kept secure settings because cleanup failed, do not manually switch to local insecure settings. |

The status report is saved at `.drivebound/status-report.json`. Share that
redacted report and the visible error when requesting help, rather than `.env`,
mail credentials, or the private configuration/recovery files.

If by “cloud” you mean a rented virtual machine, this exact guide assumes a
different host: the stationary PC with the physical disk. A VM does not gain
access to your home drive just because you connect to its desktop. The storage
must be available on the machine running the storage service, or use a separately
designed connection. Docker Desktop inside a VM has additional support and
nested-virtualization requirements; Windows Server is not supported by Docker
Desktop. Check [Docker's Windows requirements](https://docs.docker.com/desktop/setup/install/windows-install/)
before selecting that deployment.
