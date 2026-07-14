#!/bin/bash
# Native Pro M0 style guard (DESIGN-NATIVE-PRO.md §2 / §9).
#
# 1. `Color(red:` is banned everywhere except ThemeTokens.swift — every owned
#    color is a dynamic light/dark pair defined once in the token file.
# 2. Hardcoded font sizes (`system(size: <number>`) are banned outside
#    ThemeTokens.swift — everywhere, unconditionally. The legacy baselines
#    ratcheted down to zero across the design refresh and were deleted.
#
# Run from anywhere: ./ci_style_check.sh
# Also executed by swift test (StyleGuardTests).
set -uo pipefail
cd "$(dirname "$0")"
SRC="Sources/OrchestratorGUI"
# Tests/ can carry the same violations (e.g. a test hardcoding a font size to
# assert against) and CI should catch those too, not just app sources.
ROOTS=("$SRC" "Tests/OrchestratorGUITests")
STATUS=0

# --- Rule 1: Color(red: only in ThemeTokens.swift -------------------------
color_hits=$(grep -rn 'Color(red:' "${ROOTS[@]}" --include='*.swift' | grep -v '/ThemeTokens.swift:' || true)
if [ -n "$color_hits" ]; then
    echo "FAIL: Color(red:) outside ThemeTokens.swift — use a DS token:"
    echo "$color_hits"
    STATUS=1
fi

# --- Rule 2: hardcoded font sizes only in ThemeTokens.swift ----------------
# The legacy baselines are gone: the design refresh (DESIGN-REFRESH.md)
# cleared Components/ModelLibrary/TranscriptView (tranche 1) and
# Configuration.swift, the last holdout (tranche 2). The ratchet rests at 0 —
# hardcoded font sizes now fail CI everywhere, unconditionally.
for root in "${ROOTS[@]}"; do
    while IFS= read -r f; do
        rel=${f#"$root"/}
        [ "$rel" = "ThemeTokens.swift" ] && continue
        n=$(grep -cE 'system\(size:[[:space:]]*[0-9]' "$f" || true)
        if [ "$n" -gt 0 ]; then
            echo "FAIL: $rel has $n hardcoded font size(s)."
            echo "      Use the DS.font ramp in ThemeTokens.swift."
            grep -nE 'system\(size:[[:space:]]*[0-9]' "$f"
            STATUS=1
        fi
    done < <(find "$root" -name '*.swift')
done

if [ "$STATUS" -eq 0 ]; then
    echo "style check OK — no rogue Color(red:) or hardcoded font sizes."
fi
exit "$STATUS"
