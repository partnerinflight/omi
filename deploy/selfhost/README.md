# Deploying the sync API to HA_Selfhost

Not deployed yet. Deployment is a separate, explicitly scheduled step
([implementation plan](../../docs/mobile/implementation-plan.md) milestone 6).
The API stores todos in HA's shared `postgres-db`, so `./ha backup`
(`pg_dumpall`) already covers it.

1. Push the reviewed omi commit, then set `SECONDBRAIN_SERVER_COMMIT` (full SHA)
   and a generated `SECONDBRAIN_DB_PASSWORD` in the HA Infisical project.
2. In `HA_Selfhost/docker-compose.yml`: include `compose/secondbrain.yml`, and add
   networks `proxy_secondbrain` (named `${HA_COMPOSE_PROJECT_NAME}_proxy_secondbrain`)
   and internal `secondbrain_db`, each on an unused /27 like their neighbours.
   Add `secondbrain_db` to `postgresdb`'s networks in `compose/infra.yml`.
3. Create the role and database once (prompted password, not shell history):
   ```sh
   ./ha compose exec postgresdb psql -U "$POSTGRES_DB_USERNAME" -c "CREATE ROLE secondbrain LOGIN"
   ./ha compose exec postgresdb psql -U "$POSTGRES_DB_USERNAME" -c "\password secondbrain"
   ./ha compose exec postgresdb psql -U "$POSTGRES_DB_USERNAME" -c "CREATE DATABASE secondbrain OWNER secondbrain"
   ```
4. `./ha validate`, then `./ha compose --profile apps up -d secondbrain`.
   The container migrates on start. Check `https://brain.<domain>/v1/health`.
5. Pair a phone: `./ha compose exec secondbrain python -m sb_server pair "My iPhone"`,
   then enter the code in the app within 10 minutes.

After restoring a database backup, run `python -m sb_server new-epoch` in the
container so phones bootstrap again instead of trusting vanished cursors.
