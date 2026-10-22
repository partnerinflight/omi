# Deploying the sync API to HA_Selfhost

Not deployed yet. Each numbered step changes a live system and is done only with the
owner's go-ahead ([implementation plan](../../docs/mobile/implementation-plan.md) milestone 6).

Verified locally on 2026-10-10 (`apps/mobile`: `npm run test:restore`): the image builds for
linux/arm64 and linux/amd64, runs with this compose file's hardening (uid 65532, read-only
root, no capabilities), restarts after its process exits, migrates idempotently, and survives
HA's own `pg_dumpall` backup restored into a fresh postgres:14 followed by `new-epoch`, with
two phones losing nothing (edits accepted after the backup are re-sent by the phones that
hold them). Not verified: the real host, Traefik/TLS, Infisical, reachability from an iPhone.

All data (todos, memories, speaker commands, worker records, review clips) lives in the
`secondbrain` database on HA's shared `postgres-db`, so `./ha backup` already covers it;
there is no volume.

## 1. Server

1. Push the reviewed omi branch, then set `SECONDBRAIN_SERVER_COMMIT` (full SHA) and a
   generated `SECONDBRAIN_DB_PASSWORD` in the HA Infisical project.
2. Copy `secondbrain.yml` to `HA_Selfhost/compose/`. In `docker-compose.yml`, include it and
   add two networks on /27s that were unused on 2026-10-10 (re-check before applying):
   ```yaml
   proxy_secondbrain:
     name: ${HA_COMPOSE_PROJECT_NAME}_proxy_secondbrain
     internal: true
     ipam: {config: [{subnet: 172.30.5.32/27}]}
   secondbrain_db:
     internal: true
     ipam: {config: [{subnet: 172.30.5.96/27}]}
   ```
   `internal: true` on both networks: the container has no internet egress; Traefik still
   reaches it on `proxy_secondbrain`.
   Add `secondbrain_db` to `postgresdb`'s networks in `compose/infra.yml`, and
   `proxy_secondbrain` to Traefik's networks in `compose/traefik.yml` (next to `proxy_blog`).
3. Create the role and database once (prompted password, not shell history):
   ```sh
   ./ha compose exec postgresdb psql -U "$POSTGRES_DB_USERNAME" -c "CREATE ROLE secondbrain LOGIN"
   ./ha compose exec postgresdb psql -U "$POSTGRES_DB_USERNAME" -c "\password secondbrain"
   ./ha compose exec postgresdb psql -U "$POSTGRES_DB_USERNAME" -c "CREATE DATABASE secondbrain OWNER secondbrain"
   ```
   Then limit the role to its own database and a few connections (the app pools at most 5):
   ```sh
   ./ha compose exec postgresdb psql -U "$POSTGRES_DB_USERNAME" -c "ALTER ROLE secondbrain CONNECTION LIMIT 8"
   ```
   In postgres-db's `pg_hba.conf`, **above** the generic `host all all ...` lines, add:
   ```
   host  secondbrain  secondbrain  samenet  scram-sha-256
   host  all          secondbrain  all      reject
   ```
   reload (`./ha compose exec postgresdb psql -U "$POSTGRES_DB_USERNAME" -c "SELECT pg_reload_conf()"`) and
   verify the role cannot reach any other database — this must fail:
   ```sh
   ./ha compose exec postgresdb psql -U secondbrain -d postgres -h 127.0.0.1 -c "SELECT 1"
   ```
   Other apps' databases are not changed: revoking `PUBLIC` privileges there could break them.
   DNS needs no change: the `*.<domain>` wildcard and Traefik's wildcard certificate
   already cover `brain.<domain>` (checked 2026-10-10: it answers 404 from Traefik).
4. `./ha validate`. A plain `up -d secondbrain` would recreate Traefik and postgres-db
   (their networks changed), briefly interrupting every site. Instead create only this
   service, attach the live containers to its networks, then start it:
   `./ha compose --profile apps up --no-start --no-deps secondbrain`,
   `docker network connect ha_secondbrain_db postgres-db`,
   `docker network connect ha_proxy_secondbrain traefik`,
   `./ha compose --profile apps start secondbrain`. The compose files list the networks,
   so the next routine recreate matches. The container migrates on start. Check `https://brain.<domain>/v1/health` from the phone on cellular,
   not just from the LAN.
5. Pair a phone: `./ha compose exec secondbrain python -m sb_server pair "My iPhone"`, then
   enter the code in the app within 10 minutes. `sb_server devices` lists every phone,
   integration and Windows worker; `sb_server revoke-device NAME_OR_ID` cuts a phone or
   integration off immediately; `revoke-producer NAME` does the same for Windows.
6. Before exposing it, confirm on the host:
   - what `chain-no-auth@file` contains: no auth bypass beyond Google forward auth, plus
     security headers and HSTS;
   - Traefik's entrypoint timeouts: `readTimeout` is set, so slow uploads can't hold
     connections forever;
   - that Traefik is the client-facing edge. If a CDN or tunnel sits in front, the rate
     limit's `sourceCriterion` must use its client-IP header;
   - from outside, `https://brain.<domain>/docs` and `/openapi.json` return 404, and a 10 MB
     POST returns 413.
   - from a private window, `https://brain.<domain>/status` (and `/`) redirects to the Google
     login, and only `/v1/...` answers without it. The status page shows worker freshness,
     backlog counts and paired phone names, never content.

## 2. Windows bridge (shadow import, speaker review, memories)

`./ha compose exec secondbrain python -m sb_server add-producer "Home PC"`; save the token
on Windows as `C:\ProgramData\SecondBrain\config\bridge-token.txt` (service account and
admins only), set `bridge_url` and `bridge_token_file` (that absolute path) in `service.json`,
restart the service. Within a minute the tray's Receiver card shows "Server synced HH:MM"
(a failure turns the tray to Needs attention with the error), the app's Status tab shows it
reporting, and so does `https://brain.<domain>/status`.

The token file may start with the UTF-8 BOM that Windows PowerShell 5.1 `Set-Content -Encoding utf8`
writes; anything else besides the token is rejected with a clear error.

## 3. Todo cutover

Follow the runbook in the implementation plan (milestone 5). It needs steps 1–2 first.

## Restore

Restore `all_postgres.sql` as usual, then run
`./ha compose exec secondbrain python -m sb_server new-epoch`. It resets the sync epoch **and revokes
every phone, integration and Windows credential** (a dump also restores revocations as of the backup,
so a phone revoked since would otherwise work again). Pair each phone again (Status tab; queued edits
are kept and sent), create a new token for Windows with `add-producer` and reconnect it, and recreate
the Hermes integration token. Phones bootstrap again and re-send edits the backup is missing; the
Windows bridge resends its records. `new-epoch --keep-credentials` skips the revocation, for a restore
you know predates no revocation.

Memory edits
whose memory postdates the backup cannot be recovered; the app says how many.
