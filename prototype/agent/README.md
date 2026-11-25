# setuhaul-reader: the AgentCore Runtime agent

This agent hosts **only the language step**. EC2 keeps everything else: the rules reader, `validate()`, every
capacity decision, the database, the outbox, the channels and the console.

```
Twilio -> EC2 (FastAPI, unchanged) -> rules (nlu.extract)
                                      | only when the rules say UNKNOWN and no safety word
                                      v
                      AgentCore Runtime "setuhaul-reader" (this folder)
                      one structured-output call to Claude on Bedrock; no tools, no DB
EC2 <------------------ {"ok", "raw", ...} ---+
validate(raw, text, as_of) on EC2  <- the trust boundary does not move
```

## What crosses the wire

| Direction | Fields |
|---|---|
| EC2 → agent | `contract_version`, `text` (≤ 2,000 characters), `history` (last 6 turns, `role` and `text` only), `as_of` |
| agent → EC2 | `{"ok": true, "contract_version", "raw", "model", "latency_ms"}` or `{"ok": false, "contract_version", "error"}` |

No phone number, driver id, driver name or database row is ever sent. The runtime session id is
`setuhaul-reader-` + a salted SHA-256 of driver id and date (`SETUHAUL_SESSION_SALT`), so the agent can't recover who it is.

Error codes: `bad_payload`, `contract_mismatch`, `no_model`, `bad_output`, `model_error`. On any of them, and on
any timeout or AWS error, EC2's `AgentCoreFallback` returns `None` and the driver gets the usual clarifying question.

## Files

| File | Purpose |
|---|---|
| `main.py` | `BedrockAgentCoreApp` entrypoint; `handle(payload)` does the work and never raises |
| `reader_contract.py` | **Generated copy** of `setuhaul/reader_contract.py`. Don't edit it; run `build.py` |
| `build.py` | Copies the contract in. A test fails if the copies differ |
| `requirements.txt` | Pinned `bedrock-agentcore`, `langchain-aws`, `langchain-core`, `langsmith` |

## Build and test locally

```
cd prototype
python3 -I agent/build.py                    # refresh the contract copy (commit it)
python3 -I -m unittest discover -s tests -p "test_agentcore_reader.py" -v
```

To run the agent locally against Bedrock (needs AWS credentials with model access):

```
cd prototype/agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
SETUHAUL_BEDROCK_MODEL=<model or inference-profile id> AWS_REGION=<region> .venv/bin/python main.py
curl -s localhost:8080/invocations -H 'content-type: application/json' \
  -d '{"contract_version":"1","text":"gaadi kharab, ek ghante baad","history":[],"as_of":"2026-08-04T09:30:00+05:30"}'
```

## Deploy

AWS now recommends the new **AgentCore CLI** (`npm install -g @aws/agentcore`) for new projects. The Python
**starter toolkit** (`pip install bedrock-agentcore-starter-toolkit`) still deploys an existing entrypoint like
this one. Follow whichever guide is current; the starter-toolkit flow is:

```
cd prototype/agent
agentcore configure -e main.py -n setuhaul_reader -r <region> -rf requirements.txt
agentcore launch                             # builds with CodeBuild, pushes to ECR, creates the runtime
agentcore status                             # note the agent runtime ARN
```

Then:

1. **Set the model** on the runtime: environment variable `SETUHAUL_BEDROCK_MODEL` (a Claude model or inference
   profile enabled in your account and region). See `agentcore launch --help` or the runtime's settings in the console.
2. **Tighten the execution role.** It may only `bedrock:InvokeModel` (and `bedrock:InvokeModelWithResponseStream`,
   if your toolkit version streams) on that one model or inference profile. Add the CloudWatch Logs and ECR pull
   permissions the runtime itself needs, and nothing else. No S3, SSM, DynamoDB or network egress beyond Bedrock
   (and LangSmith, only if you enable it).
3. **Record the runtime ARN** in `/setuhaul/AGENTCORE_READER_ARN` (SSM) or in `settings.env` on the server.
4. **Allow EC2 to call it.** The instance role from `deploy/aws/create_role.sh` doesn't include this yet. Add one
   statement by hand:
   ```json
   {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeAgentRuntime",
    "Resource": ["<runtime ARN>", "<runtime ARN>/*"]}
   ```
   The app runs **inside Docker**, so it reaches the instance role through IMDSv2 one network hop further away.
   `deploy/aws/launch_instance.sh` sets `HttpPutResponseHopLimit=1`, and with that boto3 in the container gets
   **no credentials**. Raise the limit to 2 once:
   `aws ec2 modify-instance-metadata-options --instance-id <id> --http-tokens required --http-put-response-hop-limit 2`
5. **Turn it on, on EC2:** `READER=agentcore`, `AGENTCORE_READER_ARN=<arn>`, `AWS_REGION=<region>`,
   `SETUHAUL_SESSION_SALT=<random string, keep it in SSM>`. Then redeploy (`deploy/server/update.sh`).
6. **Check it:**
   - `/readyz` shows `checks.reader.detail = "agentcore (arn set)"`.
   - `python3 -I scripts/reader_smoke.py` prints the validated facts, or `None`, with latency.

## Roll back

Set `READER=off` (rules only) or `READER=anthropic` (the direct Claude reader with `ANTHROPIC_API_KEY`), or
unset `READER` (today's behaviour), then restart the app. Nothing else changes; no data migration.
