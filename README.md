# Expense Tracker API

Multi-currency expense tracker. FastAPI, async SQLAlchemy 2, Pydantic v2, Alembic, PostgreSQL, Redis, S3.

- **Tests:** 82 tests (real PostgreSQL + real Redis, S3 mocked with moto). **Coverage: 97%** (`pytest` prints it).
- **AI disclosure:** this project was written with Claude Code (Anthropic). I reviewed the design decisions below and can explain each of them.

## Run it

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), Docker.

```bash
cp .env.example .env            # dev values only; .env is git-ignored
docker compose up -d postgres redis   # host ports 5433 / 6380 to avoid clashing with local services
uv sync
uv run alembic upgrade head     # schema + 90 days of PKR/AED/USD/EUR rates
uv run uvicorn app.main:app --reload          # API on :8000, docs at /docs
uv run python -m app.worker                   # separate terminal: outbox + CSV imports
```

Receipts need an S3-compatible store. For local use run MinIO or LocalStack and set `S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` in `.env` and create the `S3_BUCKET`. On AWS leave the endpoint and keys unset so boto3 uses the IAM role.

```bash
uv run pytest        # one command; creates/migrates the tracker_test database itself
```
Tests need the compose Postgres and Redis running. Override with `TEST_DATABASE_URL` / `TEST_REDIS_URL`.

Postman: import `postman/expense-tracker.postman_collection.json` (covers every endpoint; `postman/sample.csv` for the import). Run folders top to bottom; variables are filled by test scripts.

## Architecture

```
app/
  auth/ categories/ expenses/ reports/ notifications/ imports/   router -> service -> DB
  outbox.py      transactional outbox (enqueue + drain + handlers)
  storage.py     ALL S3 code (boto3 in worker threads)
  worker.py      outbox drain + CSV import loop (python -m app.worker)
  errors.py      the single error format
alembic/versions/0001 schema+indexes, 0002 rate seed
```

Everything on the request path is async. Blocking libraries (argon2, boto3) run via `asyncio.to_thread`. Money is `Decimal` in Python and `NUMERIC` in PostgreSQL; the API accepts and returns amounts as exact decimals (strings in responses), never floats.

Error format everywhere: `{"error": {"code", "message", "details": []}}` (400 bad cursor, 401, 404, 409, 412, 422, 428, 429, 503).

`rate_to_usd` is defined as **USD per 1 unit of the currency** (USD = 1, PKR is about 0.0036). Converting is `amount * rate(expense currency) / rate(base currency)`.

### Decisions worth explaining

| Requirement | Implementation | Why |
|---|---|---|
| Refresh rotation | Only a SHA-256 of each token is stored. Rotation is one `UPDATE ... SET used_at WHERE used_at IS NULL RETURNING`. If it matches nothing but the token exists, it is a replay, so all of that user's tokens are revoked (committed before returning 401). | The single statement makes concurrent use race-free; hashing means a DB leak doesn't leak sessions. Already-issued access tokens stay valid up to 15 minutes (stateless JWT). |
| Login rate limit | Redis Lua script: `INCR` + `EXPIRE` atomically, key per email, 5 per 900 s, 429 + `Retry-After` from TTL. The attempt is counted before the password check; success resets it. | All API servers share one counter. Counting first means parallel guesses can't slip past the limit. If Redis is down login fails closed (503) rather than allowing unlimited guessing. |
| 404 not 403 | Every query filters on `user_id`. | Doesn't reveal that an ID exists. |
| Cursor pagination | Cursor = `(spent_on, id)`; `WHERE (spent_on, id) < (:d, :id) ORDER BY spent_on DESC, id DESC LIMIT n+1`. | Position is a value, not an offset, so inserts between pages can't shift rows. Rows inserted ahead of the cursor are simply not seen; rows behind it appear once. |
| Monthly report | One SQL statement: `LEFT JOIN LATERAL` picks the latest rate on or before the expense date (for the expense currency and the base currency), `GROUP BY category, month`, then `LAG()` over each category's months for the change. Categories with no spend still appear. Expenses with no usable rate are excluded and counted in `unconverted_expenses`. | No Python loop; gaps in rate days are handled by the `valid_on <= spent_on ORDER BY valid_on DESC LIMIT 1` lookup. `GET /categories` and the budget alerts reuse the same query, so numbers always agree. |
| Idempotency-Key | The key row is inserted in the **same transaction** as the expense with `INSERT ... ON CONFLICT (user_id, key) DO UPDATE ... WHERE created_at < now() - 24h`. A concurrent twin blocks on the primary key until the first transaction commits, then reads the stored response (or gets 422 if the body hash differs). | A DB constraint is the only thing that is atomic with the expense write. A Redis lock would add a second system that can fail and still needs the DB constraint as backstop, so I skipped it. A failed request rolls back, releasing the key. |
| No lost updates | `ETag: "<version>"`; `PATCH` needs `If-Match` (428 if absent). `UPDATE ... WHERE id AND user_id AND version = :v SET version = version + 1 RETURNING *`; zero rows means 404 or 412 (decided by one existence check). | Compare-and-swap in one statement, no read-then-write window. |
| Budget alerts | Create/update/import write an `outbox` row in the same transaction. The worker claims batches with `FOR UPDATE SKIP LOCKED`, **recomputes** the category's month total from the DB, and inserts notifications with `ON CONFLICT DO NOTHING` on `UNIQUE(user, category, month, level)`. | Recomputing makes events idempotent and order-independent; the unique key gives exactly-once alerts even if a worker crashes after the insert but before acking, or two workers run together (both tested). |
| Receipts | `POST /receipt/upload-url` returns a presigned **POST** with `content-length-range 1..5MB` and a fixed `Content-Type`. `confirm` checks the object exists, its size, and the first bytes (JPEG/PNG/PDF magic) match the extension, otherwise deletes it and returns 422. | A presigned PUT cannot enforce size; a POST policy can. Names and client content-types are not trusted. The client-supplied key must sit under `receipts/{user}/{expense}/`. |
| Receipt cleanup | Deleting an expense (or replacing its receipt) writes an `s3.delete` outbox event in the same transaction. The worker deletes the object and retries with exponential backoff until it succeeds. | If S3 is down nothing is forgotten, because the intent is stored durably in Postgres. |
| CSV import | Upload is streamed (hashed in 1 MB chunks, spooled to disk by Starlette, then to S3), a job row is created and 202 returned. The worker downloads to a temp file, reads it line by line, validates each row, inserts 1000-row batches (`INSERT ... ON CONFLICT DO NOTHING` on `(import_id, import_row)`), commits per batch so progress is visible. Bad rows go to `errors` (row number + reason, first 1000 stored; `failed` is exact). | The API process never parses the file; the worker is a separate process. Same file twice maps to the same job via `UNIQUE(user_id, sha256)`. A crashed job is re-claimed after 5 minutes and re-running is safe thanks to the per-row unique key. |

### Editing or deleting an expense and budget alerts

- An **edit** re-queues a budget check for the expense's (new) category and month, so raising an amount can trigger 80%/100%.
- A **delete** or an edit that lowers a total only reduces spend, so it can never cross a threshold upward and queues no check.
- Alerts are **history, not state**: they are not retracted when spend later drops, and they don't fire again if the total re-crosses the same level in the same month (the unique key). This avoids notification spam from edit-and-revert. A "budget recovered" message would be a separate event type.
- Moving an expense out of a category does not re-check the old category, because its total only went down.

### Indexes (each one has a query behind it)

| Index | Serves |
|---|---|
| `expenses (user_id, spent_on DESC, id DESC)` | `GET /expenses` keyset scan: matches `WHERE user_id ORDER BY spent_on DESC, id DESC`, so no sort and LIMIT stops early |
| `expenses (user_id, category_id, spent_on DESC, id DESC)` | listing filtered by category, and the report/budget scans per category and month |
| `expenses GIN (description gin_trgm_ops)` | `q` search (`ILIKE '%x%'` cannot use a btree) |
| `UNIQUE expenses (import_id, import_row) WHERE import_id IS NOT NULL` | CSV re-processing idempotency |
| `exchange_rates PK (currency, valid_on)` | the "latest rate on or before date" lateral lookup (backward index scan, LIMIT 1) |
| `outbox (id) WHERE processed_at IS NULL` | tiny partial index so the worker's claim query stays fast even with a huge processed history |
| `notifications UNIQUE (user_id, category_id, month, level)` | exactly-once alerts |
| `notifications (user_id, id DESC)` | `GET /notifications` keyset |
| `refresh_tokens UNIQUE (token_hash)`, `(user_id)` | rotation lookup; revoke-all |
| `idempotency_keys PK (user_id, key)`, `(created_at)` | claim/replay; expiry sweep |
| `import_jobs UNIQUE (user_id, file_sha256)`, `(status, updated_at)` | file dedupe; worker claim |
| `categories UNIQUE (user_id, name)`, `users UNIQUE (email)` | duplicate checks (409) |

## What I did not finish / how I would build it

- **Expired idempotency key cleanup job.** Expired keys are already ignored and overwritten on reuse; the table just isn't swept. Add a daily `DELETE FROM idempotency_keys WHERE created_at < now() - interval '24 hours'` to the worker.
- **Access-token revocation.** Access tokens are stateless JWTs (15 min), so logout / theft revocation only affects refresh tokens. A per-user `token_version` claim checked on each request would close it, at the cost of one more lookup.
- **Category update/delete endpoints and budget changes.** Not in the spec. Changing a budget would need to enqueue a re-check.
- **Orphaned uploads.** A user who gets an upload URL but never confirms leaves an object under `receipts/`. Fix with an S3 lifecycle rule (expire unreferenced objects after 1 day) or a periodic reconcile job.
- **CSV: stale S3 object.** Uploaded CSVs are kept for diagnosis; add an S3 lifecycle expiry (7 days).
- **Currencies.** Only PKR, AED, USD, EUR are accepted (config `supported_currencies`). Production would load rates daily from a provider and validate against the `exchange_rates` table.
- **Min/max amount filters** compare the raw amount in the expense's own currency (combine with `currency`); converting to base currency in the listing would need the same lateral join.
- **Real S3/MinIO not part of the automated tests.** moto covers the logic; I manually exercised presigned POST end-to-end against a moto HTTP server. The MinIO service in `docker-compose.yml` was not verified (the image could not be pulled in my environment).
- **Dockerfile and Postman collection were written but not built/executed.**

## Deployment notes (AWS)

**1. API and worker.** ECS on Fargate, two services from one image: `api` (`uvicorn`, port 8000) and `worker` (`python -m app.worker`). API: minimum 2 tasks across 2 AZs behind an ALB (health check `/health`), target-tracking autoscaling on CPU 60% and ALB request count (2 to 10 tasks). Worker: 1 to 3 tasks, scaled on outbox lag (CloudWatch metric from `count(*) WHERE processed_at IS NULL`); more workers are safe because of `SKIP LOCKED`. Failed tasks are replaced by ECS automatically. Zero-downtime release: ECS rolling deployment (min healthy 100%, max 200%): new tasks must pass the ALB health check before old ones are drained (deregistration delay 30 s, `stopTimeout` ≥ the worker's batch time). Use the ECS deployment circuit breaker with rollback. The worker handles SIGTERM by finishing its current batch.

**2. Database.** RDS PostgreSQL, Multi-AZ, in private subnets, not publicly accessible. Security group `sg-db`: inbound TCP **5432 only from `sg-app`** (the API/worker tasks' security group), no CIDR rules, no other inbound. The password is generated by RDS-managed credentials in **Secrets Manager**; the ECS task definition references the secret (`secrets: valueFrom`), so it is injected as an env var at start and never appears in the image, repo or task definition. The task role can only `secretsmanager:GetSecretValue` that one secret. Connection count: pool of 10 per task, check `max_connections` vs. max tasks (use RDS Proxy if it grows).

**3. Redis.** ElastiCache for Redis (replication group, Multi-AZ, automatic failover, in private subnets, in-transit encryption, SG allowing 6379 only from `sg-app`). If Redis is down: **login rate limiting fails closed** (login returns 503: a brief outage is better than unlimited password guessing; the alternative is fail-open with an alert, a business decision). **Idempotency keys are unaffected**: they live in Postgres, not Redis. No other feature depends on Redis. A failover resets counters, which at worst gives an attacker a fresh 5 attempts.

**4. S3.** Private bucket, Block Public Access on, SSE-S3 (or KMS) encryption, bucket versioning optional, lifecycle rules for `imports/` (7 days) and incomplete multipart uploads; CORS allowing `POST` from the app origin for browser uploads. The task role (no access keys) gets exactly:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::tracker-receipts-prod/*"
    }
  ]
}
```
(`HeadObject` is covered by `s3:GetObject`. Presigned URLs carry the signer's permissions, so the role must hold `PutObject`/`GetObject` for them to work. Add `s3:ListBucket` on the bucket ARN only if you want 404 instead of 403 for missing keys on `HEAD`.)

**5. Migrations.** Run `alembic upgrade head` as a one-off ECS task (same image, command override) in the pipeline **before** the service deployment, and fail the release if it fails. Use expand/contract so old and new code both work while rolling: release N adds nullable columns / new tables / new indexes (`CREATE INDEX CONCURRENTLY`, outside a transaction); code N+1 starts using them; a later release N+2 drops what nothing reads. Never rename or drop a column in the same release that stops using it. Take a lock timeout (`SET lock_timeout`) for DDL so a migration can't queue behind a long query and block traffic.

## Short answers

**1. Why a presigned upload URL? What can go wrong?** Streaming files through the API ties up an app worker and bandwidth per upload, makes API servers scale with file traffic, and doubles the transfer (client to API to S3). With a presigned URL the client talks to S3 directly and the API only signs and verifies. Risks: (a) the URL is a bearer credential, so keep it short-lived (5 min) and scoped to one exact key; (b) the client controls the bytes, so size and type can be forged. I use a presigned POST with a `content-length-range` and fixed `Content-Type` policy, then re-check size and the file's magic bytes on `confirm` and delete anything invalid; (c) the client could confirm someone else's key, so the key must match `receipts/{user}/{expense}/`; (d) uploads that are never confirmed become orphans, so use a lifecycle rule; (e) never serve user files inline from your own origin, so downloads use `Content-Disposition: attachment`.

**2. GET /expenses is slow at 20M rows.** First reproduce with the real query and run `EXPLAIN (ANALYZE, BUFFERS)`. I look for a Seq Scan or a Sort node over many rows, large `Buffers: shared read` (cold I/O), a big gap between estimated and actual rows (stale stats: `ANALYZE`), and "Rows Removed by Filter" (the index doesn't cover the filter). The fix is the composite index matching the query's equality-then-order columns: `(user_id, spent_on DESC, id DESC)` (plus `category_id` before the order columns for the category-filtered variant), so Postgres walks the index in order and stops after `LIMIT`, with no sort. Offset pagination is slow because `OFFSET 100000` still reads and discards 100000 index entries; cost grows linearly with page depth and rows shift under inserts. Keyset (`(spent_on, id) < (...)`) is O(limit) at any depth. For N+1: if serialization touched a relation per row (category name per expense) I'd see N identical statements in logs / `pg_stat_statements`; fix with a join or `selectinload`. Here the list response needs no per-row queries. If `q` is slow, the trigram GIN index handles it. If one user has millions of rows, consider partitioning by `spent_on`.

**3. Three servers behind a load balancer: what breaks?** *Tokens:* JWTs are stateless and refresh tokens live in Postgres, so any server can validate and rotate (rotation is one atomic UPDATE). *Rate limits:* counters are in Redis, not process memory, so the limit is global. *Worker:* multiple workers claim with `FOR UPDATE SKIP LOCKED` and handlers are idempotent (unique notification key, recompute-not-increment). *File uploads:* nothing is written to local disk: receipts go straight to S3, CSVs are stored in S3 and processed by the worker, so there is no sticky-session or shared-disk requirement. *Idempotency/locking:* both rely on DB constraints, which hold across servers. *Migrations:* run once as a separate task before rollout, expand/contract so mixed versions work, never on app start (three servers would race). Also: clock skew only matters for JWT expiry (NTP), and DB connection pool size times servers must fit `max_connections`.

**4. Outbox worker down 2 hours, 500,000 events pile up.** Nothing is lost since the events are durable rows. On restart the worker drains in batches of 500 ordered by id, using the partial index on unprocessed rows so each claim is cheap. Budget events for the same (user, category, month) inside a batch are coalesced into a single evaluation because only the latest total matters, which usually shrinks the real work dramatically. The loop sleeps 50 ms between full batches to leave DB capacity for the API, and each batch is one short transaction (so locks and WAL are bounded). Scale by adding workers (SKIP LOCKED) while watching DB CPU and connections; cap concurrency to protect the DB. Failed events back off exponentially instead of hot-looping. Users get delayed but correct alerts, because each evaluation recomputes from current data (no stale alerts). I'd alert on outbox lag so 2 hours doesn't happen unnoticed, and archive or delete processed rows periodically to keep the table small.

**5. Something simple chosen over "perfect".** The idempotency layer uses a Postgres table inside the request transaction instead of Redis locks plus a cache. A purist would put keys in Redis with `SET NX` and TTL for speed. But the database is already the source of truth for the expense, a unique key committed atomically with it removes every crash window (key stored but expense not, or the reverse), and there is one fewer system to fail. The cost is one extra indexed write per create and a table to sweep; at this scale that's cheap, and if throughput ever demanded it I could put a Redis fast path in front while keeping the table as the authority. Similarly, alerts are append-only history instead of a state machine that retracts them.
