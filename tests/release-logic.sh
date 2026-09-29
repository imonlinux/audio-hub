#!/bin/bash
# Functional test of bootstrap.sh + audiohub-update release logic with
# mocked HTTP. Run from the repo root: bash /tmp/test-release-logic.sh
set -u
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }
check() { # check <desc> <expected> <actual>
    if [ "$2" = "$3" ]; then ok "$1"; else fail "$1 (expected '$2', got '$3')"; fi
}

FIXTURES="$(mktemp -d)"
trap 'rm -rf "$FIXTURES"' EXIT

# --- fixtures ----------------------------------------------------------
mkdir -p "$FIXTURES/src/audio-hub-v1.0.0" "$FIXTURES/src/audio-hub-v1.1.0-canary"
echo "tag1" > "$FIXTURES/src/audio-hub-v1.0.0/VERSION"
echo "tag2" > "$FIXTURES/src/audio-hub-v1.1.0-canary/VERSION"
tar -czf "$FIXTURES/audio-hub-v1.0.0.tar.gz" -C "$FIXTURES/src" "audio-hub-v1.0.0"
tar -czf "$FIXTURES/audio-hub-v1.1.0-canary.tar.gz" -C "$FIXTURES/src" "audio-hub-v1.1.0-canary"
{
    sha256sum "$FIXTURES/audio-hub-v1.0.0.tar.gz" | sed "s|$FIXTURES/||"
    sha256sum "$FIXTURES/audio-hub-v1.1.0-canary.tar.gz" | sed "s|$FIXTURES/||"
} > "$FIXTURES/sha256sums-good.txt"
{ sha256sum "$FIXTURES/audio-hub-v1.1.0-canary.tar.gz" | sed "s|$FIXTURES/||"; } \
    > "$FIXTURES/sha256sums-bad.txt"

REPO="https://github.com/imonlinux/audio-hub"

# --- source a script with all network mocked ---------------------------
load_mocked() { # load_mocked <script> <sed-to-disable-main>
    source <(sed "$2" "$1")
    api_get() { # api_get <path> — prints body with HTTP code on the last line
        case "$1" in
            /releases/latest)
                [ "${MOCK_LATEST_CODE:-200}" = "404" ] && { printf '\n404'; return; }
                printf '%s\n%s' "$MOCK_LATEST_BODY" "${MOCK_LATEST_CODE:-200}"
                ;;
            /releases?per_page=1)
                [ "${MOCK_CANARY_CODE:-200}" = "000" ] && { printf '\n000'; return; }
                printf '%s\n%s' "$MOCK_CANARY_BODY" "${MOCK_CANARY_CODE:-200}"
                ;;
            *) printf '\n404' ;;
        esac
    }
    http_get() { # http_get <url> <outfile>
        local url="$1" out="$2"
        case "$url" in
            "$REPO/archive/refs/tags/"*.tar.gz)
                local tag="${url##*/tags/}"; tag="${tag%.tar.gz}"
                [ -f "$FIXTURES/audio-hub-${tag}.tar.gz" ] || return 22
                cp "$FIXTURES/audio-hub-${tag}.tar.gz" "$out" ;;
            */releases/download/*/sha256sums.txt)
                [ "${MOCK_SUMS:-sha256sums-good.txt}" = "sha256sums-missing.txt" ] && return 22
                cp "$FIXTURES/${MOCK_SUMS:-sha256sums-good.txt}" "$out" ;;
            *) return 22 ;;
        esac
    }
    API_REPO="https://api.github.com/repos/x/y"
    TARBALL_BASE="$REPO/archive"
}

mk_json() { printf '{\n  "tag_name": "%s",\n  "name": "x"\n}\n' "$1"; }

echo "== resolve_release (bootstrap.sh) =="
load_mocked bootstrap.sh 's|^main "\$@"$|:|'
MOCK_LATEST_BODY="$(mk_json v1.0.0)"; MOCK_CANARY_BODY="$(mk_json v1.1.0-canary)"
check "stable -> promoted release" "v1.0.0" "$(resolve_release stable)"
check "canary -> newest release"   "v1.1.0-canary" "$(resolve_release canary)"

MOCK_LATEST_CODE=404
check "stable falls back when nothing promoted" "v1.1.0-canary" "$(resolve_release stable)"

MOCK_LATEST_CODE=000
check "stable errors on transport failure" "" "$(resolve_release stable)"

MOCK_LATEST_CODE=200; MOCK_CANARY_CODE=000
check "canary errors when API down" "" "$(resolve_release canary)"

MOCK_CANARY_CODE=200; MOCK_CANARY_BODY='{"message": "no releases"}'
check "canary errors when no releases exist" "" "$(resolve_release canary)"

echo "== resolve_release (scripts/audiohub-update) =="
load_mocked scripts/audiohub-update 's|^if \[ "\${BASH_SOURCE\[0\]}" = "\$0" \]; then|if false; then|'
MOCK_CANARY_CODE=200; MOCK_CANARY_BODY="$(mk_json v1.1.0-canary)"
MOCK_LATEST_BODY="$(mk_json v1.0.0)"
check "stable -> promoted release" "v1.0.0" "$(resolve_release stable)"
MOCK_LATEST_CODE=404
check "stable falls back when nothing promoted" "v1.1.0-canary" "$(resolve_release stable)"

echo "== fetch_and_verify (bootstrap.sh) =="
load_mocked bootstrap.sh 's|^main "\$@"$|:|'
MOCK_SUMS="sha256sums-good.txt"
work="$(mktemp -d)"
tree="$(fetch_and_verify v1.0.0 "$work")"
check "verified tarball extracts" "tag1" "$(cat "$tree/VERSION")"
rm -rf "$work"

MOCK_SUMS="sha256sums-bad.txt"
work="$(mktemp -d)"
if tree="$(fetch_and_verify v1.0.0 "$work" 2>/dev/null)"; then
    fail "checksum mismatch must abort"
else
    ok "checksum mismatch aborts"
fi
rm -rf "$work"

MOCK_SUMS="sha256sums-missing.txt"
work="$(mktemp -d)"
if tree="$(fetch_and_verify v1.0.0 "$work" 2>/dev/null)"; then
    fail "missing checksums must abort"
else
    ok "missing checksums abort"
fi
rm -rf "$work"

echo "== audiohub-update helpers =="
export AUDIOHUB_CONFIG="$FIXTURES/envdir/unit.env"
mkdir -p "$FIXTURES/envdir"
cat > "$AUDIOHUB_CONFIG" <<'EOF'
AUDIOHUB_RELEASE_CHANNEL=canary
AUDIOHUB_AUTO_UPDATE="false"
AUDIOHUB_RELEASE=v0.9.0
EOF
load_mocked scripts/audiohub-update 's|^if \[ "\${BASH_SOURCE\[0\]}" = "\$0" \]; then|if false; then|'

check "env_value plain"        "canary" "$(env_value AUDIOHUB_RELEASE_CHANNEL)"
check "env_value quoted"       "false"  "$(env_value AUDIOHUB_AUTO_UPDATE)"
check "env_value missing"      ""       "$(env_value AUDIOHUB_NOPE)"
check "is_semver_tag yes"      "0"      "$(is_semver_tag v1.2.3; echo $?)"
check "is_semver_tag short"    "0"      "$(is_semver_tag v1.2; echo $?)"
check "is_semver_tag rc-suffix rejected" "1" "$(is_semver_tag v1.2.3-rc1; echo $?)"
check "is_semver_tag garbage rejected"   "1" "$(is_semver_tag vbanana; echo $?)"
check "tag_older 1.0<1.1"      "0"      "$(tag_older v1.0.0 v1.1.0; echo $?)"
check "tag_older 1.1>1.0"      "1"      "$(tag_older v1.1.0 v1.0.0; echo $?)"
check "tag_older equal"        "1"      "$(tag_older v1.0.0 v1.0.0; echo $?)"
check "tag_older multi-digit"  "0"      "$(tag_older v1.9.0 v1.10.0; echo $?)"

echo "== install.sh identity seeding =="
seed() { # replicate the installer's identity pipeline for a hostname
    local host="$1" id name
    id="$(printf '%s' "$host" | tr 'A-Z' 'a-z' | sed -E 's/[^a-z0-9]+/_/g; s/^_+//; s/_+$//')"
    [ -n "$id" ] || id="audio_hub"
    name="$(printf '%s' "$host" | tr 'A-Z' 'a-z' | sed -E 's/[-_.]+/ /g; s/\b([a-z])/\u\1/g')"
    printf '%s|%s|%s' "$host" "$id" "$name"
}
check "seed kitchen-media"  "kitchen-media|kitchen_media|Kitchen Media"  "$(seed kitchen-media)"
check "seed raspberrypi"    "raspberrypi|raspberrypi|Raspberrypi"        "$(seed raspberrypi)"
check "seed upPI-host"      "upPI-host|uppi_host|Uppi Host"              "$(seed upPI-host)"
check "seed dots"           "a.b--c|a_b_c|A B C"                          "$(seed a.b--c)"
check "seed 2nd-floor"      "2nd-floor|2nd_floor|2nd Floor"               "$(seed 2nd-floor)"

echo ""
echo "=== $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
