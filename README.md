# Expense Tracker API

Multi-currency expense tracker: FastAPI, async SQLAlchemy 2, Alembic, PostgreSQL, Redis, S3.

- **Tests:** 82 tests, **97% coverage** (real PostgreSQL + Redis, S3 mocked with moto).
- **AI disclosure:** written with Claude Code (Anthropic). I reviewed the design and can explain every decision below.

## Run it

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/), Docker.

```bash
cp .env.example .env                  # dev values only; .env is git-ignored
docker compose up -d postgres redis   # host ports 5433 / 6380
uv sync
uv run alembic upgrade head           # schema + 90 days of PKR/AED/USD/EUR rates
uv run uvicorn app.main:app --reload  # API on :8000, docs at /docs
uv run python -m app.worker           # second terminal: outbox + CSV imports
uv run pytest                         # creates/migrates tracker_test itself
```

Receipts need an S3-compatible store: run MinIO/LocalStack and set `S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `S3_BUCKET` in `.env`. On AWS leave endpoint and keys unset (IAM role).

Postman: import `postman/expense-tracker.postman_collection.json` (every endpoint; `postman/sample.csv` for imports) and run folders top to bottom.

## Design

Everything on the request path is async (argon2 and boto3 run in `asyncio.to_thread`). Money is `Decimal` / `NUMERIC`, never float. Errors are always `{"error": {"code", "message", "details"}}`. `rate_to_usd` is USD per 1 unit of currency; conversion is `amount * rate(expense) / rate(base)`.

| Area | Implementation | Why |
|---|---|---|
| Refresh tokens | Only the SHA-256 is stored. Rotation is one `UPDATE ... WHERE used_at IS NULL RETURNING`; a replay revokes all the user's tokens. | Race-free; a DB leak leaks no sessions. Access JWTs stay valid up to 15 min. |
| Login limit | Redis Lua `INCR`+`EXPIRE`, 5 per 15 min per email, counted before the password check. Redis down returns 503. | Shared across servers; fails closed. |
| Pagination | Keyset on `(spent_on, id)`. | No offset cost; inserts can't shift pages. |
| Report | One SQL statement: `LATERAL` latest-rate lookup, `GROUP BY`, `LAG()` for month change. Expenses with no rate are counted in `unconverted_expenses`. | No Python loop; handles gaps in rate days. |
| Idempotency | Key row inserted in the same transaction as the expense (`ON CONFLICT (user_id, key)`). | Only a DB constraint is atomic with the write; no Redis needed. |
| No lost updates | `ETag` = version; `PATCH` needs `If-Match` (428 missing, 412 stale); `UPDATE ... WHERE version = :v`. | Compare-and-swap in one statement. |
| Budget alerts | Writes enqueue an outbox row in the same transaction. Worker claims with `SKIP LOCKED`, recomputes the month total, inserts with `ON CONFLICT DO NOTHING` on `UNIQUE(user, category, month, level)`. | Idempotent, order-independent, exactly-once alerts. |
| Receipts | Presigned **POST** with size limit; `confirm` checks size and magic bytes (invalid files deleted, 422) and the key prefix. Deletes go through an `s3.delete` outbox event with retry. | A presigned PUT can't cap size; nothing is forgotten if S3 is down. |
| CSV import | Streamed to S3, job row, 202. Worker reads line by line, inserts 1000-row batches, unique `(import_id, import_row)`. Same file twice gives the same job. | API never parses the file; safe to re-run after a crash. |

**Alerts policy:** an edit re-queues a check for the new category/month; deletes and decreases queue none. Alerts are history, not state: never retracted, never repeated for the same level in a month.

**Indexes:** `expenses(user_id, spent_on DESC, id DESC)` for listing; `expenses(user_id, category_id, spent_on DESC, id DESC)` for filters and report scans; GIN trigram on `description` for `q`; unique `(import_id, import_row)`; `exchange_rates` PK `(currency, valid_on)` for the lateral lookup; partial `outbox(id) WHERE processed_at IS NULL`; unique notification key; unique `refresh_tokens(token_hash)`; PKs/uniques for idempotency, imports and categories.

## Not finished

- Expired idempotency keys are ignored but never swept; add a daily `DELETE` in the worker.
- Access tokens can't be revoked before expiry (15 min); fix with a per-user `token_version` claim.
- No category update/delete endpoints (not in spec); a budget change would need to enqueue a re-check.
- Unconfirmed uploads and old CSVs stay in S3; fix with lifecycle rules.
- Only PKR/AED/USD/EUR; production would load rates daily from a provider.
- Min/max amount filters compare the raw amount in the expense's own currency.
- Not verified: the MinIO compose service (image wouldn't pull), the Dockerfile build, the Postman collection, and real AWS S3 (tests use moto).

## Deployment (AWS)

1. **API + worker:** ECS Fargate, two services from one image. API: 2+ tasks over 2 AZs behind an ALB (`/health`), autoscaling on CPU and request count. Worker: 1–3 tasks scaled on outbox lag; safe via `SKIP LOCKED`. Rolling deploy (min healthy 100%, max 200%) with circuit-breaker rollback; the worker finishes its batch on SIGTERM.
2. **Database:** RDS PostgreSQL Multi-AZ in private subnets. Security group allows 5432 **only from the app security group**. Password is in Secrets Manager, injected through the task definition `secrets`; the task role can read that one secret. Watch `max_connections` vs. tasks × pool size (RDS Proxy if needed).
3. **Redis:** ElastiCache with failover, private, TLS, 6379 only from the app SG. If Redis is down, **login fails closed (503)** rather than allowing unlimited guessing. Idempotency lives in Postgres, so it is unaffected.
4. **S3:** private bucket, Block Public Access, encryption, lifecycle rules, CORS for `POST`. Task role (no keys) gets only:

```json
{"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
  "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
  "Resource": "arn:aws:s3:::tracker-receipts-prod/*"}]}
```

5. **Migrations:** run `alembic upgrade head` as a one-off ECS task before the rollout; fail the release if it fails. Use expand/contract (add first, drop in a later release), `CREATE INDEX CONCURRENTLY`, and a `lock_timeout`. Never on app start.

## Short answers

**1. Why a presigned upload URL? What can go wrong?** Streaming files through the API ties up workers and bandwidth and doubles the transfer. With a presigned URL the client talks to S3 directly. Risks: the URL is a bearer credential (keep it short-lived, scoped to one key); the client controls the bytes (use a POST policy with `content-length-range`, then re-check size and magic bytes on confirm); a client could confirm someone else's key (enforce the `receipts/{user}/{expense}/` prefix); unconfirmed uploads become orphans (lifecycle rule); serve downloads as `attachment`.

**2. `GET /expenses` is slow at 20M rows.** Run `EXPLAIN (ANALYZE, BUFFERS)` and look for a Seq Scan, a Sort over many rows, "Rows Removed by Filter", heavy `shared read`, and stale estimates (`ANALYZE`). The fix is the composite index `(user_id, spent_on DESC, id DESC)` (plus `category_id` for the filtered variant), so Postgres reads in order and stops at `LIMIT`. Offset pagination is slow because `OFFSET n` still reads and discards n entries, and rows shift under inserts; keyset is O(limit) at any depth. N+1 shows up as many identical statements in logs or `pg_stat_statements`; fix with a join or `selectinload`. Here the list needs no per-row queries.

**3. Three servers behind a load balancer.** JWTs are stateless and refresh tokens are in Postgres, so any server can serve any request. Rate limits are in Redis, not process memory. Workers use `SKIP LOCKED` with idempotent handlers. Nothing is written to local disk: receipts and CSVs go to S3. Idempotency and locking rely on DB constraints. Migrations run once before rollout, never on startup. Also keep clocks in sync (JWT expiry) and pool size × servers under `max_connections`.

**4. Worker down 2 hours, 500,000 events.** Nothing is lost; events are durable rows. On restart it drains 500 per batch in id order using the partial index. Budget events for the same (user, category, month) in a batch are coalesced, since only the latest total matters. It pauses 50 ms between full batches, each batch is a short transaction, and failures back off exponentially. Add workers while watching DB CPU and connections. Alerts are delayed but correct because each check recomputes from current data. I'd alert on outbox lag and archive processed rows.

**5. Simple over "perfect".** Idempotency uses a Postgres row in the expense's transaction instead of Redis locks plus a cache. That removes every crash window (key saved but expense not, or the reverse) and one system that can fail. The cost is one extra indexed write per create and a table to sweep. Redis could be added as a fast path later, with the table staying authoritative. Likewise, alerts are append-only history rather than a state machine that retracts them.
