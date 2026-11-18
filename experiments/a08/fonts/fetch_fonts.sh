#!/usr/bin/env bash
# Downloads the OFL-1.1 Noto fonts used by the A08 corpus and checks their SHA-256 (version 2.015, notofonts.github.io).
set -euo pipefail
cd "$(dirname "$0")"
B=https://raw.githubusercontent.com/notofonts/notofonts.github.io/main/fonts
while read -r sha path; do
  f=$(basename "$path"); [ -f "$f" ] || curl -sfLO "$B/$path"
  echo "$sha  $f" | shasum -a 256 -c -
done <<'LIST'
478c558ea716033cd60c03438f628dfa75694dcf6b5f6d505a2f05fd2b4f3823 NotoSans/hinted/ttf/NotoSans-Regular.ttf
1df075a380fc7cb898acf64c1f7b3b4dd780de3caa860178bf929de35817a913 NotoSans/hinted/ttf/NotoSans-Bold.ttf
467e3f89eeca4108bb8710a2b9e0cf2281ac56d5b0609211a83776d0505eecb5 NotoSans/hinted/ttf/NotoSans-Italic.ttf
806816f70ef268abf388acdacf6b2e169f58f70c9f063515eef16a1f256eace6 NotoSans/hinted/ttf/NotoSans-Condensed.ttf
19e72cd8d595fae5bd74a5206f5d938512e1183d4fed7abb1ec1be1d7efa5f88 NotoSerif/hinted/ttf/NotoSerif-Regular.ttf
96656aa5cec8f1d6fd0e804c1fad397e1a1cfa082e6642124e0bda68cd8363ce NotoSerif/hinted/ttf/NotoSerif-Bold.ttf
749e80e313ef711f9373c6cce17c72297ef05490b3dcda7967d1d5d90bf1183f NotoSerif/hinted/ttf/NotoSerif-Italic.ttf
LIST
