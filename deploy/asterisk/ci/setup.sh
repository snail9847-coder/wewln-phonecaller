#!/usr/bin/env bash
# CI (GitHub Actions, Ubuntu runner): install Asterisk from Ubuntu's packages and configure it
# with the sample files of deploy/asterisk plus the test dialplan. No SIP account is used; ARI
# listens on 127.0.0.1 only. The random ARI password goes to $GITHUB_ENV (masked in the log).
set -euo pipefail
cd "$(dirname "$0")/.."

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -q
sudo apt-get install -y -q --no-install-recommends \
  asterisk asterisk-core-sounds-en-gsm curl python3-venv
asterisk -V

password="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
echo "::add-mask::$password"
echo "ASTERISK_LIVE_ARI_PASSWORD=$password" >> "${GITHUB_ENV:-/dev/null}"

sudo cp http.conf modules.conf pjsip.conf /etc/asterisk/
sed "s/CHANGE_ME_ARI_PASSWORD/$password/" ari.conf | sudo tee /etc/asterisk/ari.conf > /dev/null
cat extensions.conf ci/extensions-ci.conf | sudo tee /etc/asterisk/extensions.conf > /dev/null
sudo chown root:asterisk /etc/asterisk/ari.conf
sudo chmod 640 /etc/asterisk/ari.conf
sudo systemctl restart asterisk

for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null -u "wewln:$password" http://127.0.0.1:8088/ari/asterisk/info; then
    break
  fi
  sleep 1
done
curl -fsS -u "wewln:$password" "http://127.0.0.1:8088/ari/asterisk/info?only=system"
echo
# The sample files load: both endpoints exist, the test dialplan is there, chan_sip is not.
# Verbose Asterisk log for the CI report (the default logs keep only notices and errors).
sudo asterisk -rx "logger add channel ci.log notice,warning,error,verbose" || true
sudo asterisk -rx "core set verbose 3" || true
sudo asterisk -rx "pjsip show endpoints" | grep -E "softphone|wewln-trunk"
sudo asterisk -rx "dialplan show wewln-ci" | grep -c "994124980"
sudo asterisk -rx "dialplan show wewln-softphone" | grep "600"
# Call audio for the voice engine: AudioSocket (externalMedia) and a mixing bridge.
sudo asterisk -rx "module show like audiosocket"
for module in chan_audiosocket.so res_audiosocket.so bridge_softmix.so; do
  if ! sudo asterisk -rx "module show like $module" | grep -q "$module"; then
    echo "$module is not loaded (needed for voice calls)" >&2
    exit 1
  fi
done
if sudo asterisk -rx "module show like chan_sip.so" | grep -q "chan_sip.so"; then
  echo "chan_sip is loaded although modules.conf says noload" >&2
  exit 1
fi
