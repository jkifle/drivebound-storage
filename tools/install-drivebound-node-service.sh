#!/usr/bin/env bash
set -euo pipefail

PACKAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../node_service" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [ ! -d "$PACKAGE_ROOT/dist" ] || [ -z "$(find "$PACKAGE_ROOT/dist" -maxdepth 1 -name 'drivebound_node_service-*.whl' -print -quit 2>/dev/null)" ]; then
  cd "$PACKAGE_ROOT"
  "$PYTHON_BIN" -m pip install --upgrade build
  "$PYTHON_BIN" -m build
fi

WHEEL="$(find "$PACKAGE_ROOT/dist" -maxdepth 1 -name 'drivebound_node_service-*.whl' | sort | tail -n 1)"
if [ -z "$WHEEL" ]; then
  echo "Unable to find a built Drivebound node wheel in $PACKAGE_ROOT/dist" >&2
  exit 1
fi

"$PYTHON_BIN" -m pip install --upgrade "$WHEEL"

echo "Drivebound node service installed successfully."
echo "Run the CLI with: python -m drivebound_node_service.cli --help"
echo "Or add the Python user Scripts path to PATH and use: drivebound-node-service"
