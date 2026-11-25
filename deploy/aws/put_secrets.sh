#!/bin/bash
# Store secrets in SSM Parameter Store as SecureString under /setuhaul/ (free tier: standard parameters,
# AWS-managed key aws/ssm). Values are typed with hidden input (read -s), never echoed and never passed as
# command-line arguments (which other processes could see). Each value goes to the AWS CLI through a private
# temporary file (mode 600, in your per-user temp folder) that is deleted right after the call, and on exit.
# (The AWS CLI on macOS cannot read --cli-input-json from /dev/stdin.)
#
#   TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN      required
#   ANTHROPIC_API_KEY, LANGSMITH_API_KEY, OPENROUTER_API_KEY, SARVAM_API_KEY, VOICE_SERVER_TOKEN   optional (press Enter to skip / keep the current value)
#   DEMO_PHONE                                 optional, personal data: your one WhatsApp number (+91XXXXXXXXXX) for
#                                              reset-demo.sh C03|C06|C07|C12|C14
#   DEMO_PHONES                                optional, personal data: several handsets, e.g. DRV013=+91...,CON003=+91...
#   SETUHAUL_DEV_SECRET                        generated randomly if missing (signs demo coordinator tokens)
# Usage: deploy/aws/put_secrets.sh [--dry-run]
. "$(dirname "$0")/common.sh"
check_aws

step "SSM SecureString parameters under $PARAM_PATH/"
cost "SecureString parameters $PARAM_PATH/{TWILIO_ACCOUNT_SID,TWILIO_AUTH_TOKEN,ANTHROPIC_API_KEY,LANGSMITH_API_KEY,OPENROUTER_API_KEY,SARVAM_API_KEY,VOICE_SERVER_TOKEN,DEMO_PHONE,DEMO_PHONES,SETUHAUL_DEV_SECRET}" \
     "US\$0.00/month (standard parameters and the aws/ssm managed key are free)"
if [ "$DRY_RUN" = 1 ]; then say "  [dry-run] would prompt for each value with hidden input and store it"; exit 0; fi
confirm

SECRET_TMP=""
trap 'rm -f "$SECRET_TMP"' EXIT INT TERM
put_secure() {   # name, value on stdin
  umask 077
  SECRET_TMP="$(mktemp "${TMPDIR:-/tmp}/setuhaul-secret.XXXXXX")"
  python3 -c 'import json,sys; print(json.dumps({"Name": sys.argv[1], "Type": "SecureString", "Overwrite": True, "Tier": "Standard", "Value": sys.stdin.read()}))' "$1" > "$SECRET_TMP"
  aws ssm put-parameter --cli-input-json "file://$SECRET_TMP" >/dev/null
  rm -f "$SECRET_TMP"; SECRET_TMP=""
}
exists() { aws ssm get-parameter --name "$1" --query Parameter.Name --output text >/dev/null 2>&1; }

ask() {   # name, required(1/0)
  local name="$1" required="$2" value=""
  local full="$PARAM_PATH/$name" note=""
  exists "$full" && note=" [already set: Enter keeps it]"
  while true; do
    read -r -s -p "  $name$note: " value; echo
    if [ -n "$value" ]; then printf '%s' "$value" | put_secure "$full"; say "    stored $full"; break; fi
    if [ -n "$note" ]; then say "    kept"; break; fi
    if [ "$required" = 0 ]; then say "    skipped"; break; fi
    say "    required: please paste it (input is hidden)"
  done
  value=""
}

ask TWILIO_ACCOUNT_SID 1
ask TWILIO_AUTH_TOKEN 1
ask ANTHROPIC_API_KEY 0
ask LANGSMITH_API_KEY 0
ask OPENROUTER_API_KEY 0
ask SARVAM_API_KEY 0
ask VOICE_SERVER_TOKEN 0
ask DEMO_PHONE 0
ask DEMO_PHONES 0
if ! exists "$PARAM_PATH/SETUHAUL_DEV_SECRET"; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' | put_secure "$PARAM_PATH/SETUHAUL_DEV_SECRET"
  say "  generated $PARAM_PATH/SETUHAUL_DEV_SECRET"
fi
say "  Done. List names (not values) with: aws ssm get-parameters-by-path --path $PARAM_PATH --query 'Parameters[].Name'"
