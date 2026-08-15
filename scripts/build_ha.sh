#!/usr/bin/env bash
set -e

# Change to the root of the repository
cd "$(dirname "$0")/.."

echo "Building Home Assistant component..."

BUILD_DIR="dist/ha_component"
COMPONENT_NAME="nem_rates"
COMPONENT_DIR="custom_components/${COMPONENT_NAME}"
VENDORED_DIR="${BUILD_DIR}/${COMPONENT_NAME}/vendored"

# Clean up previous build
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"

# Copy the custom component
cp -R "${COMPONENT_DIR}" "${BUILD_DIR}/${COMPONENT_NAME}"

# Vendor the python package
echo "Vendoring core package..."
mkdir -p "${VENDORED_DIR}"
cp -R "src/${COMPONENT_NAME}" "${VENDORED_DIR}/${COMPONENT_NAME}"

# Clean up any pycache inside the build dir
find "${BUILD_DIR}" -type d -name "__pycache__" -exec rm -rf {} +

echo "Done! The fully vendored Home Assistant component is ready at: ${BUILD_DIR}/${COMPONENT_NAME}"
echo "You can copy this folder directly to your Home Assistant's config/custom_components directory."
