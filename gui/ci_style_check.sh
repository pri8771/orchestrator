#!/bin/bash
# Native Pro M0 style guard (DESIGN-NATIVE-PRO.md §2 / §9).
#
# 1. `Color(red:` is banned everywhere except ThemeTokens.swift — every owned
#    color is a dynamic light/dark pair defined once in the token file.
# 2. Hardcoded font sizes (`system(size: <number>`) are banned outside
#    ThemeTokens.swift. Legacy files that predate the token file carry a
#    frozen baseline count below (they are §9 deletion/refactor targets);
#    the count may only ever SHRINK — new hardcoded sizes fail CI anywhere.
#
# Run from anywhere: ./ci_style_check.sh
# Also executed by swift test (StyleGuardTests).
set -uo pipefail
cd "$(dirname "$0")"
SRC="Sources/OrchestratorGUI"
STATUS=0

# --- Rule 1: Color(red: only in ThemeTokens.swift -------------------------
color_hits=$(grep -rn 'Color(red:' "$SRC" --include='*.swift' | grep -v '/ThemeTokens.swift:' || true)
if [ -n "$color_hits" ]; then
    echo "FAIL: Color(red:) outside ThemeTokens.swift — use a DS token:"
    echo "$color_hits"
    STATUS=1
fi

# --- Rule 2: hardcoded font sizes only in ThemeTokens.swift ----------------
# Frozen baselines for legacy files (pre-M0). Ratchet: counts may only shrink.
# M5 executed the §9 deletions (FactoryDashboard, the classic browser body,
# ModelsSheet/NewChatSheet/AgentSettings) — their allowances are gone and the
# survivors were re-frozen at their post-deletion counts.
baseline() {
    case "$1" in
        Components.swift) echo 2 ;;        # legacy ThemeTokens.mono + RunLogPanel header
        Configuration.swift) echo 27 ;;    # remaining settings/editor sheets
        ModelLibrary.swift) echo 9 ;;      # reworked in M3 (Models & Agents)
        TranscriptView.swift) echo 2 ;;    # restyled in M4
        *) echo 0 ;;
    esac
}

while IFS= read -r f; do
    rel=${f#"$SRC"/}
    [ "$rel" = "ThemeTokens.swift" ] && continue
    n=$(grep -cE 'system\(size:[[:space:]]*[0-9]' "$f" || true)
    allowed=$(baseline "$rel")
    if [ "$n" -gt "$allowed" ]; then
        echo "FAIL: $rel has $n hardcoded font size(s); allowed $allowed (legacy baseline)."
        echo "      Use the DS.font ramp in ThemeTokens.swift."
        grep -nE 'system\(size:[[:space:]]*[0-9]' "$f"
        STATUS=1
    fi
done < <(find "$SRC" -name '*.swift')

if [ "$STATUS" -eq 0 ]; then
    echo "style check OK — no rogue Color(red:) or hardcoded font sizes."
fi
exit "$STATUS"
