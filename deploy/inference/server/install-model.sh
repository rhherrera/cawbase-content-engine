#!/usr/bin/env bash
# Install the pinned GGUF weights: download (or copy a local file) to a temporary name on the weights volume,
# verify size and SHA-256, then rename atomically and record the verification.
#
#   install-model.sh --dir /srv/cce-inference/models                 # download from the pinned HF revision
#   install-model.sh --dir /srv/models --from /mnt/usb/Qwen3.6-27B-Q4_K_M.gguf   # on-premise: local copy, no network
#   install-model.sh --dir DIR --verify [--quick]                    # verify an installed file (quick = size + record)
#
# Overrides for tests only: CCE_MODEL_URL, CCE_MODEL_FILE, CCE_MODEL_SHA256, CCE_MODEL_BYTES.
set -euo pipefail

FILE=${CCE_MODEL_FILE:-Qwen3.6-27B-Q4_K_M.gguf}
SHA=${CCE_MODEL_SHA256:-f3ab00cc923590947877bad882e09ada67789f747f5d3cb3c178fdf91b9aaab9}
BYTES=${CCE_MODEL_BYTES:-19403683392}
URL=${CCE_MODEL_URL:-https://huggingface.co/ggml-org/Qwen3.6-27B-GGUF/resolve/fec6ff7c0c86cf07f6e5ae0a1391c3a042f2017f/Qwen3.6-27B-Q4_K_M.gguf}

DIR=""; FROM=""; MODE=install; QUICK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR=$2; shift 2 ;;
    --from) FROM=$2; shift 2 ;;
    --verify) MODE=verify; shift ;;
    --quick) QUICK=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$DIR" ] || { echo "--dir is required" >&2; exit 2; }

size_of() { stat -c %s "$1" 2>/dev/null || stat -f %z "$1"; }
sha_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
RECORD="$DIR/$FILE.verified.json"

if [ "$MODE" = verify ]; then
  [ -f "$DIR/$FILE" ] || { echo "model missing: $DIR/$FILE" >&2; exit 1; }
  [ "$(size_of "$DIR/$FILE")" = "$BYTES" ] || { echo "model size mismatch" >&2; exit 1; }
  if [ "$QUICK" = 1 ]; then
    grep -q "\"sha256\": \"$SHA\"" "$RECORD" 2>/dev/null || { echo "no verification record for $SHA" >&2; exit 1; }
    echo "model ok (quick: size + record)"; exit 0
  fi
  [ "$(sha_of "$DIR/$FILE")" = "$SHA" ] || { echo "model sha256 mismatch" >&2; exit 1; }
  echo "model ok (sha256)"; exit 0
fi

mkdir -p "$DIR"
if [ -f "$DIR/$FILE" ] && [ "$(size_of "$DIR/$FILE")" = "$BYTES" ] && [ "$(sha_of "$DIR/$FILE")" = "$SHA" ]; then
  echo "model already installed and verified"
else
  TMP="$DIR/.$FILE.partial"
  if [ -n "$FROM" ]; then
    cp "$FROM" "$TMP"
  else
    curl -fL --retry 10 --retry-delay 10 -C - -o "$TMP" "$URL"
  fi
  [ "$(size_of "$TMP")" = "$BYTES" ] || { echo "size mismatch: $(size_of "$TMP") != $BYTES" >&2; exit 1; }
  GOT=$(sha_of "$TMP")
  [ "$GOT" = "$SHA" ] || { echo "sha256 mismatch: $GOT" >&2; rm -f "$TMP"; exit 1; }
  mv -f "$TMP" "$DIR/$FILE"
  sync "$DIR/$FILE" 2>/dev/null || sync
fi
printf '{"file": "%s", "bytes": %s, "sha256": "%s", "verified_at": "%s"}\n' \
  "$FILE" "$BYTES" "$SHA" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$RECORD"
echo "installed $DIR/$FILE sha256=$SHA"
