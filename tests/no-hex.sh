#!/bin/sh
# Fails if the app's own CSS/JS/HTML contains a literal colour (#hex, rgb(), rgba(), hsl(), hsla()).
# Colours must come from the Wizard kit variables (--ink, --line, --fill, --accent, --danger, ...),
# tinted with color-mix(in srgb, var(--x) N%, transparent).
# Vendored kit files are excluded: wizard-ui.css, wizard-tokens.css, wizard-layout.css, wizard-sheet.js
# and theme-client.js (appearance client v1, byte-for-byte; see appearance-provenance.json).
set -eu
cd "$(dirname "$0")/.."
files=$(find web -type f \( -name '*.css' -o -name '*.js' -o -name '*.html' \) \
  ! -name wizard-ui.css ! -name wizard-tokens.css ! -name wizard-layout.css ! -name wizard-sheet.js ! -name theme-client.js | sort)
# A hex colour is #RGB, #RGBA, #RRGGBB or #RRGGBBAA in a colour position (after : space , ( or a quote), so
# element ids such as '#add-btn' in selectors do not count.
pattern='(^|[:[:space:],("'"'"'`])#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})([^0-9a-zA-Z_-]|$)|\b(rgba?|hsla?)\('
if hits=$(grep -nEH "$pattern" $files); then
  echo "Literal colours found (use kit variables instead):"
  echo "$hits"
  exit 1
fi
echo "no-hex: OK ($(echo "$files" | wc -l | tr -d ' ') files, no colour literals)"
