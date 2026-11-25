# Deploy SetuHaul to AWS for under US$10 a month

This runbook puts the SetuHaul WhatsApp demo on **one small server** in **Sydney** (ap-southeast-2) by default, or in **Mumbai** (ap-south-1) with HTTPS, so Twilio can reach it without ngrok and without your laptop staying on. It is written for a first-time AWS user. Each step says what it costs.

> This is a **demo / pilot-demo configuration**: one machine, SQLite, a single availability zone. It is not the production design (see `docs/deployment.md`).

> **Region.** The approved plan is Mumbai. AWS's new **Free-plan** sign-up, however, pins accounts with an Indian address to **Sydney** and blocks other regions. Mumbai needs the **Paid plan + "Activate advanced features"** (AWS Settings).
> - The scripts default to Sydney.
> - After upgrading, put `export SETUHAUL_REGION=ap-south-1` before the commands to use Mumbai.
> - Everything else is identical.

## What you get, and what it costs

| Item | Mumbai ap-south-1 (plan; Paid plan needed) | Sydney ap-southeast-2 (Free plan default) |
|---|---|---|
| EC2 **t4g.micro** (2 vCPU arm64, 1 GiB RAM), CPU credits *standard* | ~$4.09 ($0.0056/h) | ~$7.74-8.03 ($0.0106-0.011/h; sources differ) |
| **Elastic IP** (public IPv4), **billed even when stopped** | ~$3.65 | ~$3.65 |
| **EBS gp3 10 GiB**, encrypted | ~$0.91 | ~$0.96 |
| **S3** backups (30 days) | ~$0.01-0.10 | ~$0.01-0.10 |
| IAM, security group, SSM, CloudWatch alarm, Budget | $0 | $0 |
| **Total** | **~$8.75-8.85** | **~$12.4-12.7: above the $10 plan** |

Prices are on-demand list prices checked 2026-11-20 (third-party sources for EC2; verify in the AWS Pricing Calculator). **On the Free plan, nothing is charged to you:** credits pay, and the account pauses at 6 months or when the credits run out. Sydney for 6 months is about $75, within the $100+ credits.

Nothing else is created: no load balancer, NAT gateway, RDS, ElastiCache, Fargate, Secrets Manager or CloudFront. With new-account Free Tier credits ($100, plus up to $100 more), this costs nothing until the credits run out.

```
 Driver's WhatsApp -> Twilio Sandbox --HTTPS--> Elastic IP : 443
                                                   |
            EC2 t4g.micro (Amazon Linux 2023, Docker Compose)
            +-- caddy  (HTTPS certificate; only /webhook/twilio, /webhook/twilio/status, /healthz are public)
            +-- app    (FastAPI, 1 uvicorn worker, SQLite on /opt/setuhaul/data, outbox worker inside)
            +-- redis  (sessions, dedupe, locks; 64 MB cap)
            nightly backup -> private S3 bucket (30-day expiry); secrets <- SSM Parameter Store
```

---

## Steps

Each step takes a few minutes. Every script first prints **what it will create and the monthly cost**, then waits until you type `yes`. Every script can be run again safely: it reuses what exists. Add `--dry-run` to any AWS script to see the plan without changing anything.

### 1. Make the AWS account safe (once, about 10 minutes)
1. Create the account at **aws.amazon.com/free**. Choose the **Free plan**, which cannot charge you; you can upgrade later and keep your credits.
2. **Turn on MFA for the root user:** top-right menu → *Security credentials* → *Assign MFA device*.
3. Check your project's region at **settings.aws.com → Project → Additional info**. With a Free-plan account from India it is **Sydney (ap-southeast-2)**. Keep the console on that region for every step below.
4. Don't create access keys. You'll use **CloudShell**, the terminal inside the AWS console, which is already logged in.

### 2. Put the deploy files on GitHub (on your laptop)
The server downloads the code from your public fork, so push the branch first:
```
git push -u fork whatsapp-api
```
(Or use VS Code → Source Control → … → Push to… → `fork`.)

### 3. Open CloudShell and get the scripts
In the AWS console (in your project's region), click the **CloudShell** icon (`>_`) in the top bar. Or use this Mac's AWS CLI after `aws login --region ap-southeast-2`. Then:
```
git clone -b whatsapp-api https://github.com/Shanny-dot/SetuHaul_Logistics_Management.git
cd SetuHaul_Logistics_Management
```

Mumbai only (Paid plan with advanced features): run `export SETUHAUL_REGION=ap-south-1` first.

### 4. Create the server's permission role ($0)
```
deploy/aws/create_role.sh
```
This lets the server use Session Manager (no SSH), read only `/setuhaul/*` secrets, and only *write* backups to the bucket.

### 5. Create the backup bucket (~$0.10/month)
```
deploy/aws/create_bucket.sh
```
The bucket is private and encrypted, accepts HTTPS only, and deletes backups after 30 days.

### 6. Store your secrets ($0)
```
deploy/aws/put_secrets.sh
```
Paste each value when asked. **Nothing appears on screen**, and nothing is saved in a file or in shell history.
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`: from the Twilio Console home page.
- `ANTHROPIC_API_KEY`, `LANGSMITH_API_KEY`: optional; press Enter to skip.
- `DEMO_PHONE`: your WhatsApp number, e.g. `+91XXXXXXXXXX`, for the one-phone demo.
- `DEMO_PHONES`: optional, for several handsets, e.g. `DRV013=+91…,CON003=+91…`.

### 7. Launch the server (~$12.4/month in Sydney, ~$8.65 in Mumbai)
```
deploy/aws/launch_instance.sh
```
At the end it prints your **hostname**, for example `13-233-45-67.sslip.io`, and the two Twilio URLs. **Write them down.**

On first boot the server installs Docker, adds 2 GiB of swap, downloads the code, builds the app and starts it. This takes **about 5-10 minutes**.

### 8. Check the server is healthy
1. EC2 console → **Instances** → `setuhaul` → **Connect** → **Session Manager** → **Connect**. A terminal opens in your browser.
2. Run:
```
sudo tail -n 30 /var/log/setuhaul-bootstrap.log       # should end with "DEPLOYED <commit>" and "bootstrap finished"
sudo /opt/setuhaul/src/deploy/server/healthcheck.sh
```
You want `OK /readyz` and `OK https://<host>/healthz`. If HTTPS says WARN, wait a minute (the certificate is being issued) and run it again.

### 9. Point Twilio at the server
Twilio Console → Messaging → Try it out → Send a WhatsApp message → **Sandbox settings**:

| Field | Value | Method |
|---|---|---|
| When a message comes in | `https://<your-host>/webhook/twilio` | POST |
| Status callback URL | `https://<your-host>/webhook/twilio/status` | POST |

Click **Save**. Each test phone must have sent `join <code>` to +1 415 523 8886 within the last 3 days.

### 10. Run a demo scenario
In the Session Manager terminal:
```
sudo /opt/setuhaul/src/deploy/server/reset-demo.sh C03
```
This gives you a fresh 4 Aug world, maps your phone to driver DRV013, and sets the clock to 09:19. Then, on WhatsApp, send `I am late by one hour.` to +1 415 523 8886 and reply `1`.

Other scenarios work the same way: `C06`, `C07`, `C12`, `C14`. See `docs/twilio_live_test.md` for what to send and what to expect.

### 11. Turn on the safety nets ($0)
Back in CloudShell:
```
deploy/aws/create_alarm_and_budget.sh
```
- A **CloudWatch alarm** recovers the server automatically if AWS hardware fails. You keep the same IP and the same disk.
- An **AWS Budget** of $10 emails you when real usage passes **$8** or **$10**, or when the forecast passes $10. Credits are excluded, so you see what the server really costs even while credits pay for it.
- In Sydney the list price (~$12.4) is above $10, so those emails will arrive every month. Use `BUDGET_USD=15 deploy/aws/create_alarm_and_budget.sh` for alerts that mean "something unexpected".

---

## Running it day to day

### Look at the coordinator side (private)
The console API and `/dev/*` are **not** reachable from the internet; only the two webhooks and `/healthz` are. Use them inside a Session Manager terminal:
```
TOKEN=$(curl -s localhost:8000/auth/dev-login -H 'content-type: application/json' \
  -d '{"user_id":"neha.bansal@setuhaul.example"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s localhost:8000/console/holds -H "Authorization: Bearer $TOKEN"
curl -s -X POST localhost:8000/console/holds/HLD-0001/decide -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"approve":true}'
curl -s localhost:8000/console/outbox -H "Authorization: Bearer $TOKEN"
```

### Update to the latest code
1. On your laptop, commit and push to the fork.
2. On the server:
```
sudo /opt/setuhaul/src/deploy/server/update.sh
```
This pulls, builds a new image, restarts, and checks `/readyz`. **If `/readyz` fails within 2 minutes, it rolls back** to the previous commit and image on its own.

A restart resets the demo clock, so run `reset-demo.sh` afterwards for a clean demo.

To roll back by hand: `sudo cat /opt/setuhaul/APP_TAG` shows the current image. To go back, run `cd /opt/setuhaul/src && sudo git reset --hard <older-commit> && sudo /opt/setuhaul/src/deploy/server/update.sh --no-pull`.

### Backups and restore (test this once)
Backups protect your **data**, not uptime. A backup runs **every night at 02:30 IST** and goes to `s3://setuhaul-backups-<account>-ap-south-1/backups/`.

1. **Take one now:** `sudo /opt/setuhaul/src/deploy/server/backup.sh`. It prints `integrity=ok` and the S3 path.
2. **Test a restore without touching the live database.** In the S3 console, open the bucket → `backups/` → pick a file → **Download**. Then, in CloudShell (Actions → Upload file) or on your laptop:
   ```
   deploy/server/restore-check.sh demo_world-XXXXXXXXTXXXXXXZ.db.gz
   ```
   It restores into a scratch copy and prints `integrity_check: ok`, `booking guards: all 3 present` and row counts (it fails if a guard trigger is missing). The server role can only *write* backups (least privilege), which is why you download through the console.
3. **Real restore.** Put the file on the server (it has the last 3 local copies in `/opt/setuhaul/backups/`), then:
   ```
   cd /opt/setuhaul/src/deploy/server && sudo docker compose --env-file /opt/setuhaul/compose.env stop app
   sudo sh -c 'gunzip -c /opt/setuhaul/backups/<file>.db.gz > /opt/setuhaul/data/demo_world.db && chown 10001:10001 /opt/setuhaul/data/demo_world.db && rm -f /opt/setuhaul/data/demo_world.db-wal /opt/setuhaul/data/demo_world.db-shm'
   sudo docker compose --env-file /opt/setuhaul/compose.env start app && sudo ./healthcheck.sh --wait 60
   ```

### Memory (1 GiB box): confirm nothing was killed
```
sudo docker stats --no-stream          # caddy <128 MiB, app <512 MiB, redis <96 MiB
free -m                                 # 2 GiB swap is there
sudo dmesg | grep -i -E 'out of memory|oom'   # no output = no process killed for memory
```
`healthcheck.sh` reports all of these too.

### HTTPS hostname (sslip.io, DuckDNS or your own domain)
The default is `<ip-with-dashes>.sslip.io`, which needs no signup. Caddy gets a free Let's Encrypt certificate and keeps it on disk, so restarts don't request a new one.

If certificate issuance fails (Let's Encrypt limits certificates per domain, and sslip.io is shared by many people), use your own name:
1. Get a free name at **duckdns.org** and point it at your Elastic IP.
2. On the server: `echo 'DOMAIN=myname.duckdns.org' | sudo tee -a /opt/setuhaul/settings.env`.
3. Run `sudo /opt/setuhaul/src/deploy/server/update.sh --no-pull`.
4. Paste the new URLs into Twilio.

`TWILIO_WEBHOOK_BASE_URL` always follows `DOMAIN`, and **it must match the URL in Twilio exactly**, or Twilio's signature check fails and the app answers 403.

### Pausing vs deleting
- **Stopping** the instance (EC2 → Instance state → Stop) saves ~$4.09 but **not** the Elastic IP's ~$3.65 or the disk's ~$0.91.
- **To stop all charges**, run **`deploy/aws/teardown.sh`** in CloudShell. It releases the Elastic IP and terminates the server. It then asks separately before deleting the disk and the backup bucket, so you can keep your data. Type `delete setuhaul` to confirm.
- Afterwards check **EC2 → Elastic IPs / Volumes** and **Billing → Bills**.

---

## Production hardening (all inside the same US\$10 box)

| Area | What is in place | Where |
|---|---|---|
| Access | No SSH key, no port 22; Session Manager only. IMDSv2 only, hop limit 1, so containers can't reach the instance's credentials | `launch_instance.sh` |
| Network | Only 80/443 open; only the two Twilio webhooks and `/healthz` reach the app; everything else returns 404 | security group, `Caddyfile` |
| TLS | Automatic Let's Encrypt certificate; HSTS, `nosniff`, `X-Frame-Options`, no `Server` header | `Caddyfile` |
| Edge limits | 256 KB request body cap; read/write/idle timeouts | `Caddyfile` |
| Twilio | Every webhook signature checked before any processing; the exact public URL is enforced | app + `load-env.sh` |
| Secrets | SSM SecureString; `.env` is root-only (600); never printed; values typed hidden | `put_secrets.sh`, `load-env.sh` |
| Containers | App runs as uid 10001 with a **read-only filesystem**, all Linux capabilities dropped and `no-new-privileges`; memory caps on all three containers | `docker-compose.yml`, `Dockerfile` |
| Disk | Docker logs rotate (10 MB × 3 per container); journald capped at 200 MB | `bootstrap.sh` |
| Patching | `dnf-automatic` applies security updates daily. **Reboot monthly by hand**: `sudo reboot`, then `reset-demo.sh` | `bootstrap.sh` |
| Recovery | Docker restarts crashed containers; the **watchdog** restarts a hung app after 3 failed `/readyz` checks; the CloudWatch alarm recovers the instance on hardware failure; Docker `live-restore` keeps containers running through a Docker update | `watchdog.sh`, `create_alarm_and_budget.sh` |
| Deploys | Image per commit; automatic rollback if `/readyz` fails | `update.sh` |
| Data | Nightly integrity-checked backup to S3 (30 days); `healthcheck.sh` warns if the last backup is older than 26 h; restore procedure tested | `backup.sh`, `restore-check.sh` |
| Cost | Budget emails at \$8 / \$10; `teardown.sh` releases the Elastic IP | `create_alarm_and_budget.sh`, `teardown.sh` |

What "production-ready" does **not** mean here: one machine in one availability zone, SQLite, no high availability, and logs only on the box. The production design for real pilot volume is the managed-Postgres path in the project documents.

Logs: `/var/log/setuhaul-bootstrap.log`, `/var/log/setuhaul-backup.log`, `/var/log/setuhaul-watchdog.log`, and `sudo docker compose … logs app`.

## If something goes wrong

| Symptom | Check | Fix |
|---|---|---|
| WhatsApp messages get no reply | Twilio Console → Monitor → Logs → Errors | **11200 + 403**: the Sandbox URL doesn't exactly match `https://<DOMAIN>/webhook/twilio`. **63015**: the phone must re-join the sandbox. |
| `healthcheck.sh`: `/readyz` FAIL | `sudo docker compose --env-file /opt/setuhaul/compose.env -f /opt/setuhaul/src/deploy/server/docker-compose.yml logs --tail 50 app` | Missing secret: re-run `put_secrets.sh`, then `sudo …/load-env.sh && sudo …/update.sh --no-pull` |
| HTTPS WARN for more than 10 minutes | `… logs caddy` | Certificate rate-limited or port 80 blocked: use a DuckDNS `DOMAIN` (see above) |
| "Connect" button greyed out | Instance → Security → IAM role must be `setuhaul-ec2-role` | Re-run `create_role.sh`, then reboot the instance |
| Reply is about the wrong trip or time | Demo clock | `sudo …/reset-demo.sh C03` (or the scenario you want) |
| Budget email arrives | Billing → Bills | Run `teardown.sh` if the demo is over |

## What you must do by hand (checklist)
- [ ] Create the AWS account (Free plan), turn on root MFA, select Mumbai
- [ ] Push `whatsapp-api` to your fork (step 2)
- [ ] Run steps 4-7 and 11 in CloudShell, typing `yes` at each prompt
- [ ] Paste the two webhook URLs into Twilio's Sandbox settings and **Save** (step 9)
- [ ] Join the sandbox (`join <code>`) on every test phone, at least once every 3 days
- [ ] Take and restore-check one backup (Backups, steps 1-2)
- [ ] When finished: `deploy/aws/teardown.sh`, then check Billing

## Files
| Path | What it does |
|---|---|
| `aws/common.sh` | Shared helpers: Mumbai region, `--dry-run`, cost line, `yes` prompt |
| `aws/create_role.sh` | IAM role + instance profile (SSM core + least-privilege inline policy) |
| `aws/create_bucket.sh` | Private S3 backup bucket, 30-day lifecycle, TLS-only; saves its name in SSM |
| `aws/put_secrets.sh` | Hidden prompts → SSM SecureString `/setuhaul/*` (values go via stdin, never arguments) |
| `aws/launch_instance.sh` | Security group (80/443), t4g.micro (IMDSv2, standard credits, 10 GiB gp3 encrypted, no key), Elastic IP |
| `aws/create_alarm_and_budget.sh` | Auto-recover alarm; $10 budget with alerts at $8/$10 |
| `aws/teardown.sh` | Releases the EIP, terminates the server; asks before deleting the disk and the bucket |
| `server/bootstrap.sh` | First boot: Docker, compose/buildx (checksums verified), swap, folders, cron, first deploy |
| `server/Dockerfile`, `entrypoint.sh` | python:3.11-slim, wheels only, 1 uvicorn worker with proxy headers; creates the demo DB copy |
| `server/docker-compose.yml`, `Caddyfile` | caddy + app + redis with memory caps; only the webhooks and `/healthz` are public |
| `server/load-env.sh` | Builds `/opt/setuhaul/.env` (chmod 600) from the template, settings and SSM |
| `server/update.sh`, `healthcheck.sh` | Deploy with automatic rollback; health, memory, OOM and disk report |
| `server/backup.sh`, `sqlite_backup.py`, `setuhaul-backup.cron`, `restore-check.sh` | Nightly backup to S3; restore test |
| `server/reset-demo.sh` | Fresh demo world, phones mapped, clock set per scenario |
| `server/watchdog.sh`, `setuhaul-watchdog.cron` | Every 5 minutes: restart the app after 3 failed `/readyz` checks |
| `.env.template` | Every variable the app reads, with demo defaults (no secrets) |
| `tests/local_smoke.sh` | No-Docker test: real Caddyfile + app + signed webhook + backup/restore |
| `tests/smoke.sh` | Docker test: builds the arm64 image and runs the real compose stack |
