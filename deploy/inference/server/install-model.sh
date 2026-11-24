#!/usr/bin/env bash
# Install or verify the GGUF weights of a model pinned in manifest.json (the only source of revision, file, URL,
# exact size and SHA-256). Download (or copy a local file) to a temporary name on the weights volume, verify size
# and SHA-256, then rename atomically and record the verification.
#
#   install-model.sh --model qwen3.6-27b-q8_0 --dir /srv/cce-inference/models          # download (AWS target)
#   install-model.sh --model qwen3.6-27b-q4km --dir /srv/models --from /mnt/usb/Qwen3.6-27B-Q4_K_M.gguf
#                                                                       # on-premise: local copy, no network
#   install-model.sh --model ID --dir DIR --verify [--quick]            # verify (quick = size + record)
#   install-model.sh --check --model ID --path FILE --alias ALIAS       # before starting the server: the
#       configured file and alias are the manifest's for ID and the file passes the quick verification
#   install-model.sh --model ID --field file|alias|bytes|sha256|url|quantization   # print one manifest field
#   install-model.sh --target aws|onprem                                # print the model id of a target
#
# Any mismatch (unknown model, other file or alias, size, SHA-256, missing record, not enough space) fails
# explicitly with a non-zero exit. Override for tests only: CCE_MANIFEST.
set -euo pipefail

MANIFEST=${CCE_MANIFEST:-$(cd "$(dirname "$0")/.." && pwd)/manifest.json}
MODEL=""; DIR=""; FROM=""; MODE=install; QUICK=0; FIELD=""; TARGET=""; CPATH=""; CALIAS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL=$2; shift 2 ;;
    --dir) DIR=$2; shift 2 ;;
    --from) FROM=$2; shift 2 ;;
    --verify) MODE=verify; shift ;;
    --quick) QUICK=1; shift ;;
    --check) MODE=check; shift ;;
    --path) CPATH=$2; shift 2 ;;
    --alias) CALIAS=$2; shift 2 ;;
    --field) MODE=field; FIELD=$2; shift 2 ;;
    --target) MODE=target; TARGET=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

manifest() {  # manifest KIND KEY [FIELD]: one value from manifest.json, or exit 3 with the reason on stderr
  python3 -I - "$MANIFEST" "$@" <<'PY'
import json, sys
path, kind, key, *field = sys.argv[1:]
try:
    m = json.load(open(path, encoding="utf-8"))
except (OSError, ValueError) as exc:
    sys.exit(f"manifest unreadable: {path}: {exc}")
if kind == "target":
    val = m.get("targets", {}).get(key)
    if not val:
        sys.exit(f"no model for target {key!r} in {path}")
    print(val)
    sys.exit(0)
model = m.get("models", {}).get(key)
if model is None:
    sys.exit(f"unknown model {key!r}: not in {path} (known: {', '.join(sorted(m.get('models', {})))})")
val = model.get(field[0])
if val in (None, ""):
    sys.exit(f"model {key!r} has no {field[0]!r} in {path}")
print(val)
PY
}

if [ "$MODE" = target ]; then manifest target "$TARGET"; exit 0; fi
[ -n "$MODEL" ] || { echo "--model is required (a model id of manifest.json)" >&2; exit 2; }
if [ "$MODE" = field ]; then manifest model "$MODEL" "$FIELD"; exit 0; fi

FILE=$(manifest model "$MODEL" file)
SHA=$(manifest model "$MODEL" sha256)
BYTES=$(manifest model "$MODEL" bytes)
ALIAS=$(manifest model "$MODEL" alias)
case "$SHA" in *[!0-9a-f]*|"") echo "manifest sha256 of $MODEL is not a hex digest" >&2; exit 3 ;; esac
[ "${#SHA}" = 64 ] || { echo "manifest sha256 of $MODEL is not 64 hex digits" >&2; exit 3; }
case "$BYTES" in *[!0-9]*|"") echo "manifest bytes of $MODEL is not an integer" >&2; exit 3 ;; esac

size_of() { stat -c %s "$1" 2>/dev/null || stat -f %z "$1"; }
sha_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

quick_ok() {  # DIR: size + a verification record for exactly this file, size and SHA-256
  local d=$1
  [ -f "$d/$FILE" ] || { echo "model missing: $d/$FILE ($MODEL)" >&2; return 1; }
  [ "$(size_of "$d/$FILE")" = "$BYTES" ] || { echo "model size mismatch: $d/$FILE is not $BYTES bytes ($MODEL)" >&2; return 1; }
  grep -q "\"file\": \"$FILE\", \"bytes\": $BYTES, \"sha256\": \"$SHA\"" "$d/$FILE.verified.json" 2>/dev/null \
    || { echo "no verification record for $FILE sha256=$SHA ($MODEL)" >&2; return 1; }
}

if [ "$MODE" = check ]; then
  [ -n "$CPATH" ] && [ -n "$CALIAS" ] || { echo "--check needs --path and --alias" >&2; exit 2; }
  [ "$(basename "$CPATH")" = "$FILE" ] \
    || { echo "artifact mismatch: CCE_MODEL_PATH is $(basename "$CPATH"), $MODEL is $FILE" >&2; exit 1; }
  [ "$CALIAS" = "$ALIAS" ] \
    || { echo "artifact mismatch: CCE_MODEL_ALIAS is $CALIAS, $MODEL is $ALIAS" >&2; exit 1; }
  quick_ok "$(dirname "$CPATH")" || exit 1
  echo "model $MODEL ok: $FILE alias=$ALIAS (size + record)"; exit 0
fi

[ -n "$DIR" ] || { echo "--dir is required" >&2; exit 2; }
RECORD="$DIR/$FILE.verified.json"

if [ "$MODE" = verify ]; then
  if [ "$QUICK" = 1 ]; then quick_ok "$DIR" || exit 1; echo "model $MODEL ok (quick: size + record)"; exit 0; fi
  [ -f "$DIR/$FILE" ] || { echo "model missing: $DIR/$FILE ($MODEL)" >&2; exit 1; }
  [ "$(size_of "$DIR/$FILE")" = "$BYTES" ] || { echo "model size mismatch ($MODEL)" >&2; exit 1; }
  [ "$(sha_of "$DIR/$FILE")" = "$SHA" ] || { echo "model sha256 mismatch ($MODEL)" >&2; exit 1; }
  echo "model $MODEL ok (sha256)"; exit 0
fi

mkdir -p "$DIR"
if [ -f "$DIR/$FILE" ] && [ "$(size_of "$DIR/$FILE")" = "$BYTES" ] && [ "$(sha_of "$DIR/$FILE")" = "$SHA" ]; then
  echo "model $MODEL already installed and verified"
else
  rm -f "$RECORD" "$DIR/$FILE"  # an unverifiable file is unusable; never keep it next to the new download
  TMP="$DIR/.$FILE.partial"
  have=0; [ -f "$TMP" ] && have=$(size_of "$TMP")
  avail_kib=$(df -Pk "$DIR" | awk 'NR == 2 {print $4}')
  need_kib=$(( (BYTES - have) / 1024 + 65536 ))  # what is left to write, plus 64 MiB of headroom
  [ "$avail_kib" -ge "$need_kib" ] \
    || { echo "not enough space in $DIR for $MODEL: need $need_kib KiB, have $avail_kib KiB" >&2; exit 1; }
  if [ -n "$FROM" ]; then
    cp "$FROM" "$TMP"
  else
    curl -fL --retry 10 --retry-delay 10 -C - -o "$TMP" "$(manifest model "$MODEL" url)"
  fi
  [ "$(size_of "$TMP")" = "$BYTES" ] || { echo "size mismatch: $(size_of "$TMP") != $BYTES ($MODEL)" >&2; rm -f "$TMP"; exit 1; }
  GOT=$(sha_of "$TMP")
  [ "$GOT" = "$SHA" ] || { echo "sha256 mismatch: $GOT != $SHA ($MODEL)" >&2; rm -f "$TMP"; exit 1; }
  mv -f "$TMP" "$DIR/$FILE"
  sync "$DIR/$FILE" 2>/dev/null || sync
fi
printf '{"model": "%s", "file": "%s", "bytes": %s, "sha256": "%s", "alias": "%s", "verified_at": "%s"}\n' \
  "$MODEL" "$FILE" "$BYTES" "$SHA" "$ALIAS" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$RECORD.tmp"
mv -f "$RECORD.tmp" "$RECORD"
echo "installed $DIR/$FILE ($MODEL) sha256=$SHA"
