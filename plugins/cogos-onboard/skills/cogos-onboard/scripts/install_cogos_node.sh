#!/bin/sh
# install_cogos_node.sh — scaffold a user-tier CogOS node (~/.cogos) and seed it
# from the operator's own harvested history. Ships NO agent and NO operator data.
#
# Usage:
#   sh install_cogos_node.sh --conversation FILE [--owner NAME] [--agent NAME] [--home DIR]
#
#   --conversation FILE   harvested conversation JSON ({"turns":[{"role","text"}...]})
#   --owner NAME          room owner (default: the operator; falls back to "operator")
#   --agent NAME          room's agent identity name (default: the agent-from-history
#                         skill mints this; a placeholder is fine until then)
#   --home DIR            node parent dir (default: $HOME). Node lives at DIR/.cogos
#
# The seeder scripts (seed_observatory.py, seed_room.py) ship ALONGSIDE this script
# and are found relative to it. POSIX sh only: no bashisms, no brace expansion.
# Idempotent: safe to re-run. Never clobbers an existing node's keys/rooms/identity.

set -eu

CONV=""
OWNER="operator"
AGENT="identity_operator"
HOME_DIR="${HOME:-}"

die() { echo "install_cogos_node: error: $*" >&2; exit 1; }
info() { echo "install_cogos_node: $*"; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --conversation) [ "$#" -ge 2 ] || die "--conversation needs a value"; CONV="$2"; shift 2 ;;
        --owner)        [ "$#" -ge 2 ] || die "--owner needs a value"; OWNER="$2"; shift 2 ;;
        --agent)        [ "$#" -ge 2 ] || die "--agent needs a value"; AGENT="$2"; shift 2 ;;
        --home)         [ "$#" -ge 2 ] || die "--home needs a value"; HOME_DIR="$2"; shift 2 ;;
        -h|--help)      sed -n '2,19p' "$0"; exit 0 ;;
        *)              die "unknown argument: $1" ;;
    esac
done

[ -n "$CONV" ]     || die "--conversation is required"
[ -n "$HOME_DIR" ] || die "no home directory (set --home or HOME)"
[ -f "$CONV" ]     || die "conversation JSON not found: $CONV"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

# Seeder scripts ship alongside this script.
SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SCRIPTS_DIR/seed_observatory.py" ] || die "seed_observatory.py not found next to this script"
[ -f "$SCRIPTS_DIR/seed_room.py" ]        || die "seed_room.py not found next to this script"

NODE="$HOME_DIR/.cogos"
info "node tier: $NODE"

# ---- 1. Scaffold the user tier (POSIX for-loop, NOT brace expansion) -------
for d in bin config/identity hooks mem state/conversations logs org stage; do
    mkdir -p "$NODE/$d"
done
info "scaffold: ok"

STATE="$NODE/bootstrap-state.json"
printf '%s\n' \
  '{"bootstrap_version":"0.1.0","phases":{' \
  '"scaffold":"done",' \
  '"observatory":"done",' \
  '"room":"done"' \
  '}}' > "$STATE.tmp"
mv "$STATE.tmp" "$STATE"

# ---- 2. Seed observatory + room from the operator's OWN history -------------
# seed_observatory.py writes a deterministic <source>__<session>.json (same bytes on re-run).
OBS_DIR="$NODE/state/conversations"
python3 "$SCRIPTS_DIR/seed_observatory.py" "$CONV" "$OBS_DIR" || die "seed_observatory.py failed"

# seed_room.py is additive and no-ops when its room id already exists.
ROOMS="$NODE/state/rooms.json"
python3 "$SCRIPTS_DIR/seed_room.py" "$CONV" "$ROOMS" --owner "$OWNER" --agent "$AGENT" \
    || die "seed_room.py failed"

info "done. node at $NODE"
info "next: mint the operator's agent from this history (agent-from-history skill), then identity keys"
