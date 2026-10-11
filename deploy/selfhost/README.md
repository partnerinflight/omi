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
     ipam: {config: [{subnet: 172.30.5.32/27}]}
   secondbrain_db:
     internal: true
     ipam: {config: [{subnet: 172.30.5.96/27}]}
   ```
   Add `secondbrain_db` to `postgresdb`'s networks in `compose/infra.yml`, and
   `proxy_secondbrain` to Traefik's networks in `compose/traefik.yml` (next to `proxy_blog`).
3. Create the role and database once (prompted password, not shell history):
   ```sh
   ./ha compose exec postgresdb psql -U "$POSTGRES_DB_USERNAME" -c "CREATE ROLE secondbrain LOGIN"
   ./ha compose exec postgresdb psql -U "$POSTGRES_DB_USERNAME" -c "\password secondbrain"
   ./ha compose exec postgresdb psql -U "$POSTGRES_DB_USERNAME" -c "CREATE DATABASE secondbrain OWNER secondbrain"
   ```
4. `./ha validate`, then `./ha compose --profile apps up -d secondbrain`. The container
   migrates on start. Check `https://brain.<domain>/v1/health` from the phone on cellular,
   not just from the LAN.
5. Pair a phone: `./ha compose exec secondbrain python -m sb_server pair "My iPhone"`, then
   enter the code in the app within 10 minutes.

## 2. Windows bridge (shadow import, speaker review, memories)

`./ha compose exec secondbrain python -m sb_server add-producer "Home PC"`; save the token
on Windows as `C:\ProgramData\SecondBrain\config\bridge-token.txt` (service account and
admins only), set `bridge_url` in `service.json`, restart the service. The tray status and
the app's pipeline section should show it reporting within a minute.

## 3. Todo cutover

Follow the runbook in the implementation plan (milestone 5). It needs steps 1–2 first.

## Restore

Restore `all_postgres.sql` as usual, then run
`./ha compose exec secondbrain python -m sb_server new-epoch`. Phones bootstrap again and
re-send edits the backup is missing; the Windows bridge resends its records. Memory edits
whose memory postdates the backup cannot be recovered; the app says how many.
