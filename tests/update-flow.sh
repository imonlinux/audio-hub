#!/bin/bash
# End-to-end simulation of scripts/audiohub-update main flow with mocked
# network, paths, and root check. Run from the repo root.
set -u
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else fail "$1 (expected '$2', got '$3')"; fi; }

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

# --- fixture release tarballs ------------------------------------------
mkrelease() { # mkrelease <tag>
    mkdir -p "$T/src/audio-hub-$1"
    echo "payload-$1" > "$T/src/audio-hub-$1/PAYLOAD"
    {
        echo '#!/bin/bash'
        echo 'echo "install user=$AUDIOHUB_USER from=$(pwd)" >> '"$T"'/install.log'
        [ "$1" = "$FAILTAG" ] && echo 'exit 1'
        echo 'exit 0'
    } > "$T/src/audio-hub-$1/install.sh"
    tar -czf "$T/audio-hub-$1.tar.gz" -C "$T/src" "audio-hub-$1"
}
FAILTAG="v9.9.9-fail"
mkrelease v1.0.0
mkrelease v1.1.0
mkrelease v1.2.0
mkrelease "$FAILTAG"
{ sha256sum "$T"/audio-hub-*.tar.gz | sed "s|$T/||"; } > "$T/sha256sums.txt"

# --- deployed unit state ------------------------------------------------
export AUDIOHUB_CONFIG="$T/etc/audiohub/unit.env"
mkdir -p "$T/opt/audio-hub" "$(dirname "$AUDIOHUB_CONFIG")" "$T/etc/audiohub"
echo "payload-v1.0.0" > "$T/opt/audio-hub/PAYLOAD"
echo "v1.0.0" > "$T/opt/audio-hub/.release-tag"
echo "v1.0.0" > "$T/etc/audiohub/version"
cat > "$AUDIOHUB_CONFIG" <<EOF
AUDIOHUB_USER=$(id -un)
AUDIOHUB_RELEASE_CHANNEL=stable
AUDIOHUB_AUTO_UPDATE=true
EOF

# --- load the updater with paths/root/network mocked --------------------
sed -e "s|^INSTALL_ROOT=.*|INSTALL_ROOT=\"$T/opt/audio-hub\"|" \
    -e "s|/etc/audiohub/version|$T/etc/audiohub/version|g" \
    -e 's|^ *if \[ "\${EUID:-\$(id -u)}" -ne 0 \]; then|    if false; then|' \
    -e 's|^    sudo -u "\$u" -H env|    env|' \
    scripts/audiohub-update > "$T/update-mocked.sh"
source "$T/update-mocked.sh"
set +e +o pipefail   # harness: un-lease -e from the sourced updater
API_REPO="https://api.github.com/repos/x/y"
TARBALL_BASE="$T/archive"
api_get() {
    case "$1" in
        /releases/latest) printf '%s\n200' "$(printf '{"tag_name": "%s"}' "$MOCK_TAG")" ;;
        /releases*per_page=1) printf '%s\n200' "$(printf '{"tag_name": "%s"}' "${MOCK_CANARY_TAG:-$MOCK_TAG}")" ;;
        *) printf '\n404' ;;
    esac
}
http_get() {
    local tag="${1##*/tags/}"; tag="${tag%.tar.gz}"
    [ "${1##*/}" = "sha256sums.txt" ] && { cp "$T/sha256sums.txt" "$2"; return 0; }
    [ -f "$T/audio-hub-${tag}.tar.gz" ] && { cp "$T/audio-hub-${tag}.tar.gz" "$2"; return 0; }
    return 22
}
sudo() { env "$@"; }   # as_hub path when sed misses (belt and braces)

echo "== successful update v1.0.0 -> v1.1.0 =="
MOCK_TAG="v1.1.0"
MODE="manual"
out="$(main --force 2>&1)"; rc=$?
check "update exits 0" "0" "$rc"
check "new payload deployed"    "payload-v1.1.0" "$(cat "$T/opt/audio-hub/PAYLOAD")"
check "release-tag updated"     "v1.1.0"         "$(cat "$T/opt/audio-hub/.release-tag")"
check "version stamp updated"   "v1.1.0"         "$(cat "$T/etc/audiohub/version")"
check "old tree kept"           "v1.0.0"         "$(cat "$T/opt/audio-hub.old/.release-tag")"
check "installer ran as hub user" "install user=$(id -un) from=$T/opt/audio-hub" "$(cat "$T/install.log")"

echo "== up-to-date short-circuit =="
rm -f "$T/install.log"
MODE="manual"
out="$(main)" ; rc=$?
check "already-current exits 0" "0" "$rc"
[ -f "$T/install.log" ] && fail "installer must not run when current" || ok "installer not run when current"

echo "== scheduled forward-only guard =="
MOCK_TAG="v1.0.0"
out="$(main --scheduled 2>&1)"; rc=$?
check "older release skipped"   "0" "$rc"
grep -q "forward-only\|older than deployed" <<<"$out" && ok "skip reason logged" || fail "no skip reason in: $out"
check "tree untouched"          "payload-v1.1.0" "$(cat "$T/opt/audio-hub/PAYLOAD")"

echo "== scheduled mode honors AUTO_UPDATE=false =="
echo "AUDIOHUB_AUTO_UPDATE=false" >> "$AUDIOHUB_CONFIG"
MODE="scheduled"
out="$(main --scheduled 2>&1)"; rc=$?
check "disabled exits 0" "0" "$rc"
grep -qi "disabled" <<<"$out" && ok "says why" || fail "no disabled note in: $out"
sed -i '/AUDIOHUB_AUTO_UPDATE=false/d' "$AUDIOHUB_CONFIG"

echo "== failed install rolls back =="
MOCK_TAG="$FAILTAG"
MODE="manual"
out="$(main 2>&1)"; rc=$?
check "failed update exits nonzero" "1" "$rc"
check "old tree restored"       "payload-v1.1.0" "$(cat "$T/opt/audio-hub/PAYLOAD")"
check "release-tag restored"    "v1.1.0"         "$(cat "$T/opt/audio-hub/.release-tag")"
check "version stamp restored"  "v1.1.0"         "$(cat "$T/etc/audiohub/version")"
grep -q "install user=" <(tail -1 "$T/install.log") && ok "rollback re-ran installer" || fail "no rollback install in log"

# --- HA settings.json overrides (spec Section 4.7) -----------------------
export AUDIOHUB_HUB_HOME="$T/hubhome"
mkdir -p "$AUDIOHUB_HUB_HOME/.config/audiohub" "$T/run"
STATUS="$T/run/update-status.json"
echo "AUDIOHUB_AUTO_UPDATE=false" >> "$AUDIOHUB_CONFIG"

echo "== settings.json auto_update=true overrides unit.env false =="
echo '{"values": {"auto_update": true}}' > "$AUDIOHUB_HUB_HOME/.config/audiohub/settings.json"
MOCK_TAG="v1.2.0"
MODE="scheduled"
out="$(main --scheduled 2>&1)"; rc=$?
check "HA opt-in runs the scheduled update" "0" "$rc"
check "tree updated via HA preference" "payload-v1.2.0" "$(cat "$T/opt/audio-hub/PAYLOAD")"
check "version stamp updated"    "v1.2.0"         "$(cat "$T/etc/audiohub/version")"

echo "== settings.json auto_update=false overrides unit.env true =="
echo '{"values": {"auto_update": false}}' > "$AUDIOHUB_HUB_HOME/.config/audiohub/settings.json"
MODE="scheduled"
out="$(main --scheduled 2>&1)"; rc=$?
check "HA opt-out skips" "0" "$rc"
grep -qi "disabled" <<<"$out" && ok "says why" || fail "no disabled note in: $out"

echo "== absent settings.json falls back to unit.env =="
rm -f "$AUDIOHUB_HUB_HOME/.config/audiohub/settings.json"
MODE="scheduled"
out="$(main --scheduled 2>&1)"; rc=$?
check "unit.env AUTO_UPDATE=false still honored" "0" "$rc"
grep -qi "disabled" <<<"$out" && ok "fallback reads unit.env" || fail "no disabled note in: $out"

echo "== release_channel: settings.json canary overrides unit.env stable =="
echo '{"values": {"release_channel": "canary"}}' > "$AUDIOHUB_HUB_HOME/.config/audiohub/settings.json"
MOCK_TAG="v1.2.0"; MOCK_CANARY_TAG="v1.1.0"
MODE="manual"
rm -f "$STATUS"
out="$(main --check --status-file "$STATUS" 2>&1)"; rc=$?
check "canary check exits 3 when stale" "3" "$rc"
grep -q "resolves to v1.1.0" <<<"$out" && ok "channel came from settings.json" || fail "canary not used: $out"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d["installed_version"]=="v1.2.0" and d["latest_version"]=="v1.1.0" and d["token"] is None and d["error"] is None else 1)' "$STATUS" \
    && ok "daily-check status: availability without token" || fail "bad check status: $(cat "$STATUS")"

echo '{"values": {"release_channel": "stable"}}' > "$AUDIOHUB_HUB_HOME/.config/audiohub/settings.json"
out="$(main --check --status-file "$STATUS" 2>&1)"; rc=$?
check "stable check exits 0 when current" "0" "$rc"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d["latest_version"]=="v1.2.0" and d["installed_version"]=="v1.2.0" else 1)' "$STATUS" \
    && ok "stable channel resolves the deployed tag" || fail "bad stable status: $(cat "$STATUS")"

echo "== requested install echoes the request token (already current) =="
rm -f "$STATUS"
MOCK_TAG="v1.2.0"
out="$(main --status-file "$STATUS" --request-token abc123def 2>&1)"; rc=$?
check "already-current run exits 0" "0" "$rc"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d["token"]=="abc123def" and d["error"] is None else 1)' "$STATUS" \
    && ok "completion token echoed" || fail "token not echoed: $(cat "$STATUS")"

echo "== failed requested install echoes token with the error =="
rm -f "$STATUS"
MOCK_TAG="$FAILTAG"
MODE="manual"
out="$(main --status-file "$STATUS" --request-token feedface 2>&1)"; rc=$?
check "failed update exits nonzero" "1" "$rc"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d["token"]=="feedface" and d["installed_version"]=="v1.2.0" and "install failed" in d["error"] else 1)' "$STATUS" \
    && ok "failure status echoes token + old version" || fail "bad failure status: $(cat "$STATUS")"
check "tree restored after failed request" "payload-v1.2.0" "$(cat "$T/opt/audio-hub/PAYLOAD")"

# leave the late sections untouched: no settings.json, default channel
rm -f "$AUDIOHUB_HUB_HOME/.config/audiohub/settings.json"
unset AUDIOHUB_HUB_HOME
sed -i '/AUDIOHUB_AUTO_UPDATE=false/d' "$AUDIOHUB_CONFIG"

echo "== check mode =="
MOCK_TAG="v1.1.0"
MODE="manual"
echo "v1.1.0" > "$T/etc/audiohub/version"
out="$(main --check)"; rc=$?
check "check exits 0 when current" "0" "$rc"
echo "v1.0.0" > "$T/etc/audiohub/version"
out="$(main --check)"; rc=$?
check "check exits 3 when stale" "3" "$rc"

echo ""
echo "=== $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
