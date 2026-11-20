#!/usr/bin/env bash
# Install the pinned llama.cpp runtime (Linux x64, CUDA) from the official release, verifying SHA-256 and size.
#
#   sudo deploy/inference/server/install-runtime.sh [--prefix /opt/cce-inference] [--variant cuda-13.5|cuda-12.8]
#   CCE_RUNTIME_CACHE=/path/with/tarballs ...   # use already downloaded tarballs (still verified)
#
# Result: $PREFIX/runtime/llama-b12329 (binaries) and $PREFIX/runtime/cudart (CUDA runtime libraries), plus
# $PREFIX/runtime/current -> llama-b12329. Only the NVIDIA driver is needed on the host.
set -euo pipefail

PREFIX=/opt/cce-inference
VARIANT=cuda-13.5
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX=$2; shift 2 ;;
    --variant) VARIANT=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

TAG=b12329
BASE=https://github.com/ggml-org/llama.cpp/releases/download/$TAG
case "$VARIANT" in
  cuda-13.5)
    MAIN=llama-$TAG-bin-ubuntu-cuda-13.5-x64.tar.gz; MAIN_SHA=efd2f8a8da31a9f2dc432bc9c4e8c2aca495d4811f817d7936e73ad8cbbce88f; MAIN_BYTES=177032917
    RT=cudart-llama-$TAG-bin-ubuntu-cuda-13.5-x64.tar.gz; RT_SHA=5b34aefc86df6e8890694866c803da5ebeac5f8f277a5d069d9a9f123ab22bd9; RT_BYTES=444078936 ;;
  cuda-12.8)
    MAIN=llama-$TAG-bin-ubuntu-cuda-12.8-x64.tar.gz; MAIN_SHA=09b350ea374593bd5bb4257a5cbcb813a9db4b729f104c0afaa53dd2dab052de; MAIN_BYTES=198059792
    RT=cudart-llama-$TAG-bin-ubuntu-cuda-12.8-x64.tar.gz; RT_SHA=07b91f774120763dad5587394beb8ca1f14a2981a44071a8f82deaae136ab9e9; RT_BYTES=594371572 ;;
  *) echo "unknown variant: $VARIANT" >&2; exit 2 ;;
esac

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

fetch() {  # name sha bytes
  local name=$1 sha=$2 bytes=$3 dst="$WORK/$1"
  if [ -n "${CCE_RUNTIME_CACHE:-}" ] && [ -f "$CCE_RUNTIME_CACHE/$name" ]; then
    cp "$CCE_RUNTIME_CACHE/$name" "$dst"
  else
    curl -fsSL --retry 5 --retry-delay 5 -o "$dst" "$BASE/$name"
  fi
  local size; size=$(stat -c %s "$dst" 2>/dev/null || stat -f %z "$dst")
  [ "$size" = "$bytes" ] || { echo "size mismatch for $name: $size != $bytes" >&2; exit 1; }
  echo "$sha  $dst" | sha256sum -c --quiet - || { echo "sha256 mismatch for $name" >&2; exit 1; }
  echo "verified $name sha256=$sha"
}

fetch "$MAIN" "$MAIN_SHA" "$MAIN_BYTES"
fetch "$RT" "$RT_SHA" "$RT_BYTES"

DEST="$PREFIX/runtime"
STAGE="$DEST/.stage-$$"
rm -rf "$STAGE"; mkdir -p "$STAGE"
tar -xzf "$WORK/$MAIN" -C "$STAGE"
tar -xzf "$WORK/$RT" -C "$STAGE"
[ -x "$STAGE/llama-$TAG/llama-server" ] || { echo "llama-server not found in $MAIN" >&2; exit 1; }
rm -rf "$DEST/llama-$TAG" "$DEST/cudart"
mv "$STAGE/llama-$TAG" "$DEST/llama-$TAG"
mv "$STAGE"/cudart-llama-* "$DEST/cudart"
rmdir "$STAGE"
ln -sfn "llama-$TAG" "$DEST/current"

# Shared libraries the release does not ship: libgomp.so.1 (OpenMP, package libgomp1) and libcuda.so.1 (NVIDIA
# driver). Missing libgomp is an installation error; missing libcuda only means no driver on this machine.
missing=$(LD_LIBRARY_PATH="$DEST/current:$DEST/cudart" ldd "$DEST/current/llama-server" "$DEST/current/libggml-cuda.so" 2>/dev/null \
  | awk '/not found/{print $1}' | sort -u | tr '\n' ' ')
case " $missing " in *" libgomp.so.1 "*) echo "missing libgomp.so.1: install the libgomp1 package" >&2; exit 1 ;; esac
case " $missing " in *" libcuda.so.1 "*) echo "warning: libcuda.so.1 not found (NVIDIA driver not installed here)" >&2 ;; esac
cat > "$DEST/runtime.json" <<EOF
{"tag": "$TAG", "variant": "$VARIANT", "artifacts": {"$MAIN": "$MAIN_SHA", "$RT": "$RT_SHA"}}
EOF
echo "installed $DEST/llama-$TAG ($VARIANT)"
