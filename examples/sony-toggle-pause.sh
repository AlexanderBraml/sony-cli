#!/bin/bash
# Toggle Sony headphones between Noise Cancelling and Ambient Sound.
# Pass "pause" to also pause the music when switching to Ambient Sound
# and resume it when switching back to Noise Cancelling.
#
# Usage: sony-toggle-pause.sh [pause]

# Use sony-cli from PATH if installed, otherwise the script from this repo.
if command -v sony-cli >/dev/null 2>&1; then
    SONY_CLI="sony-cli"
else
    SONY_CLI="$(dirname "$(readlink -f "$0")")/../sony-cli.py"
fi
PLAYER="${PLAYER:-spotify}"

# First argument: pass "pause" to also control music playback
control_music="$1"

# toggle prints the new mode, e.g. "Ambient Sound" or "Noise Cancelling"
mode="$("$SONY_CLI" toggle)" || exit 1

if [ "$control_music" = "pause" ]; then
    case "$mode" in
        "Ambient Sound")    playerctl --player="$PLAYER" pause ;;
        "Noise Cancelling") playerctl --player="$PLAYER" play ;;
    esac
fi
