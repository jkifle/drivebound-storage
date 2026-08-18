# Drivebound node service

This package provides a background node runtime that can be installed on other desktops and paired with a Drivebound control plane.

Installation:

Build the package and install it on the external device:

```bash
cd node_service
python -m pip install --upgrade pip build
python -m build
python -m pip install dist/drivebound_node_service-*.whl
```

For a one-click setup on Windows or Linux, use the helper installer scripts in the repo:

```powershell
./tools/install-drivebound-node-service.ps1
```

```bash
./tools/install-drivebound-node-service.sh
```

If the script installs the package without making the CLI visible on PATH, use:

```bash
python -m drivebound_node_service.cli --help
```

Alternatively, for local development from this repo:

```bash
python -m pip install ./node_service
```

Pair a drive:

```bash
drivebound-node-service pair --server http://localhost:8000 --pair-code ABCD1234 --name "Basement drive"
```

Run the node as a background service:

```bash
drivebound-node-service run --config ~/.drivebound-node/config.json
```

Generate an OS-specific service file for installation:

```bash
drivebound-node-service install --platform auto --config ~/.drivebound-node/config.json
# or explicitly:
drivebound-node-service install --platform linux --config ~/.drivebound-node/config.json
# on Windows:
drivebound-node-service install --platform windows --config %USERPROFILE%\.drivebound-node\config.json
```

When run as root on Linux, the installer writes the service file to `/etc/systemd/system/drivebound-node.service`. On Windows it writes a helper launcher script that can be registered in Task Scheduler or NSSM.

The node periodically reports its health and detected storage mounts back to the Drivebound API, and it now exposes authenticated read/write storage endpoints for a physical-drive cloud storage backend.

Supported storage operations:

- `GET /api/v1/files/roots` returns the allowed mounted storage roots.
- `GET /api/v1/files/browse?path=...` enumerates files and folders inside an allowed root.
- `GET /api/v1/files/download?path=...` downloads a file securely.
- `POST /api/v1/files/upload?path=...&overwrite=true` writes a file into an allowed root with path traversal protection.

Security defaults:

- The HTTP file API binds to loopback unless `--allow-public-access` is explicitly enabled.
- Direct node access requires the `X-Drivebound-Node-Auth` header matching the paired node secret.
- Filesystem browsing is restricted to configured storage roots and blocks path traversal outside those roots.
- Public node access should always use HTTPS behind a reverse proxy or TLS terminator.
