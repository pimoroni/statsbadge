#!/usr/bin/env bash
#
# Put the Developer ID certificate somewhere codesign can use it, and the notarisation
# credentials somewhere briefcase can find them. Everything after this is `briefcase
# package --identity`.
#
# Reads from the environment, all of them from the release environment's secrets:
#
#   IDENTITY              the signing identity's name, brackets and all
#   CERTIFICATE           the certificate and its key, a base64 .p12
#   CERTIFICATE_PASSWORD  that file's export password
#   APPLE_ID              the Apple ID the app-specific password belongs to
#   APPLE_PASSWORD        the app-specific password
#
# A script and not a run: block, so it can be read as shell, and run against a real
# certificate on a Mac before anyone waits on a release to find out.
set -euo pipefail

: "${IDENTITY:?the name of the signing identity}"
: "${CERTIFICATE:?the base64 .p12}"
: "${CERTIFICATE_PASSWORD:?the .p12 password}"
: "${APPLE_ID:?the Apple ID for notarisation}"
: "${APPLE_PASSWORD:?the app-specific password}"

# The name, not the 40-character hash `security find-identity` prints beside it. Briefcase
# takes either, but only the name carries the team ID, and the profile stored below has to
# be named for the team briefcase will go looking for.
TEAM_ID="$(sed -n 's/.*(\([0-9A-Z]*\)).*/\1/p' <<<"$IDENTITY")"
if [ -z "$TEAM_ID" ]; then
  echo "IDENTITY is '$IDENTITY', which carries no team ID." >&2
  echo "Use the quoted name from security find-identity -v -p codesigning," >&2
  echo "without the quotes, not the hash beside it." >&2
  exit 1
fi

# A keychain of this run's own. The password is throwaway: the runner is destroyed with it.
KEYCHAIN="${RUNNER_TEMP:-$TMPDIR}/signing.keychain-db"
PASSWORD="$(uuidgen)"
PKCS12="${RUNNER_TEMP:-$TMPDIR}/certificate.p12"
trap 'rm -f "$PKCS12"' EXIT

security create-keychain -p "$PASSWORD" "$KEYCHAIN"
security set-keychain-settings -lut 21600 "$KEYCHAIN"
security unlock-keychain -p "$PASSWORD" "$KEYCHAIN"

base64 --decode <<<"$CERTIFICATE" >"$PKCS12"
security import "$PKCS12" -k "$KEYCHAIN" -P "$CERTIFICATE_PASSWORD" \
  -T /usr/bin/codesign -T /usr/bin/security

# Without this, codesign asks to use the key the first time and blocks forever.
security set-key-partition-list -S apple-tool:,apple:,codesign: \
  -s -k "$PASSWORD" "$KEYCHAIN" >/dev/null
# Ahead of the login keychain, which is what codesign and notarytool search.
security list-keychains -d user -s "$KEYCHAIN" login.keychain-db

echo "Signing identities now available:"
security find-identity -v -p codesigning "$KEYCHAIN"

# briefcase looks for a profile under exactly this name and prompts for the credentials
# where it finds none, which on a runner means hanging until the job times out.
echo "Storing notarisation credentials for team $TEAM_ID"
if ! xcrun notarytool store-credentials "briefcase-macOS-$TEAM_ID" \
     --team-id "$TEAM_ID" --apple-id "$APPLE_ID" --password "$APPLE_PASSWORD" \
     --keychain "$KEYCHAIN"; then
  # A 403 here means the team ID and the Apple ID do not go together, which is worth
  # spelling out: an Apple ID can belong to several teams, and only a Developer ID
  # certificate carries its team in the brackets. On an Apple Development one those hold a
  # per-certificate id and the team is in OU, so a team ID read off the wrong certificate
  # is a plausible-looking value that authenticates against nothing.
  echo >&2
  echo "Storing credentials for team $TEAM_ID failed." >&2
  echo "That team came from the brackets in '$IDENTITY'." >&2
  echo "Check it against the OU of the certificate itself:" >&2
  echo "  security find-certificate -c '$IDENTITY' -p | openssl x509 -noout -subject" >&2
  echo "and against the Membership page of the account the Apple ID belongs to." >&2
  exit 1
fi
