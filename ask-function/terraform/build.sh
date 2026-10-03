#!/usr/bin/env bash
# Build the Lambda package: pin-installed deps + the function source, zipped.
#
# mitchella and anthropic are installed from requirements.txt (mitchella is
# pinned by git ref); boto3 is in the Lambda runtime and is deliberately NOT
# vendored. Terraform runs this via null_resource.build whenever a source file
# or requirements.txt changes; you can also run it by hand before a local apply.
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$MODULE_DIR/../src"
REQUIREMENTS="$MODULE_DIR/../requirements.txt"
BUILD_DIR="$MODULE_DIR/build"
STAGE_DIR="$BUILD_DIR/package"

rm -rf "$STAGE_DIR" "$BUILD_DIR/package.zip"
mkdir -p "$STAGE_DIR"

python3 -m pip install \
  --quiet --no-compile \
  --target "$STAGE_DIR" \
  -r "$REQUIREMENTS"

cp "$SRC_DIR"/*.py "$STAGE_DIR"/

# Deterministic zip (sorted, fixed timestamps) so an unchanged build is byte-stable.
( cd "$STAGE_DIR" && find . -exec touch -t 200001010000 {} + && \
  zip -qrX "$BUILD_DIR/package.zip" . )

echo "built $BUILD_DIR/package.zip"
