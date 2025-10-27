# Accounts Module Overview

This document describes the architecture of the `backend.apps.accounts` package, covering identity, RBAC, authentication, subscription management, credit accounting, and the APIs the frontend consumes.

## 1. Identity & RBAC

| Component | Path | Notes |
| --- | --- | --- |
| `User` | `models.py` | Custom auth user keyed by UUID/email. Provides cached permission resolution (`permission_codes`) and token version bumping. |
| `AccessPermission` / `Role` | `models.py` | Fine-grained permission codes grouped into roles; roles can inherit other roles. |
| `UserRole` | `models.py` | Assignment join with optional expiration. Signals refresh cached permissions. |
| `UserAccountMeta` | `models.py` | Security metadata (token version, MFA state, default role) plus subscription fields (`plan`, `pending_plan`, cycle timestamps). |

Signals (`signals.py`) bootstrap `UserAccountMeta`, assign the default role (`settings.ACCOUNTS_DEFAULT_ROLE_SLUG`) and initialize the subscription state via `ensure_subscription_state` whenever a user is created.

## 2. Authentication & Sessions

* **JWT Auth** – `authentication.JWTAuthentication` decodes access tokens issued by `services.issue_login_tokens`. Tokens embed roles/permissions and `token_version` for revocation.
* **Refresh Tokens** – `RefreshToken` model hash-stores opaque refresh tokens. Revocation, rotation, and audit logging are in `services.py`.
* **Endpoints** – `api/views.AuthViewSet` exposes `/register`, `/login`, `/refresh`, `/logout`, and `/me` under `/api/accounts/auth/`.
  * `logout` is idempotent, clears the `refresh_token` cookie, and always returns `200 OK`.
  * `/auth/me` now reports `user`, `plan`, `pending_plan`, `credits`, and status flags via `AccountProfileSerializer`.

## 3. Subscription & Credit Ledger

### Data Model

| Model | Purpose |
| --- | --- |
| `PlanTier` | SaaS plan metadata (slug, price, monthly credits, concurrency limit, feature list). |
| `BillingCycle` | 30-day (configurable) cycle snapshot storing plan, start/end, allocations, rollover and bonus amounts. |
| `CreditLedgerEntry` | Individual credit buckets (monthly allocation, rollover, promo, top-up, adjustment) with remaining balance and optional expiration. |
| `CreditUsageLog` | Audit trail of consumption events, referencing the cycle, reason, and source summary. |

### Service Layer (`subscriptions.py`)

* `ensure_subscription_state()` – Guarantees a user has an active plan, current billing cycle, and monthly ledger entry. Creates rollover allocations when cycles renew.
* `credit_snapshot()` – Aggregates totals and breakdowns (promo, rollover, monthly, add-on) for `/auth/me`.
* `spend_credits()` – Consumes credits following the priority order: promo → rollover → current monthly → add-on. Raises `InsufficientCredits` if insufficient balance; logs usage.
* `grant_top_up()` / `apply_plan_upgrade()` / `schedule_plan_downgrade()` – Helpers for add-on packs and plan transitions. Upgrades grant prorated bonus credits using the provided formula.
* `CreditSnapshot` dataclass drives API serialization.

### Migrations

* `0002_...` – Creates plan, cycle, ledger, and usage log tables plus subscription fields on `UserAccountMeta`.
* `0003_seed_default_plans` – Seeds `Free` (100 credits, concurrency 1, default) and `Plus` (2,000 credits, concurrency 2) plans, initializing existing users with a cycle + monthly ledger entry.

## 4. Concurrency Enforcement

`concurrency.py` enforces per-plan concurrency limits on AI searches:

* Uses Redis sorted sets keyed by `acct:concurrency:<user_id>`.
* `search_concurrency_guard()` is an async context manager that acquires/releases a slot within the configured TTL (`settings.ACCOUNTS_CONCURRENCY_WINDOW_SECONDS`, default 60 s).
* `RetrievalRunner.run()` wraps the entire retrieval workflow in this guard and emits a realtime error event if the user exceeds their limit (Free → 1, Plus → 2).

## 5. API Surface

| Endpoint | Description |
| --- | --- |
| `GET /api/accounts/auth/me/` | Returns user info, plan tier metadata, pending plan, credit breakdown (`total`, `remaining`, `promo`, `rollover`, `monthly`, `add_on`, `cycle_start/end`, `next_reset_at`), and status flags. |
| `POST /api/accounts/auth/logout/` | Revokes submitted refresh token (if provided), clears the cookie, idempotent 200 response. |
| `GET /api/accounts/stats/` | Placeholder usage endpoint returning counts for `tests_generated`/`favorites_saved` and the reporting period (supports `?range=billing_cycle`, `7d`, `30d`, `custom:<days>`). |
| `Role` & `Membership` routers | Existing RBAC management endpoints protected by `HasPermissionCode`. |

Future credit-consuming endpoints should call `spend_credits()` once the AI search/generation succeeds and capture `reference_type/reference_id` for auditability.

## 6. Configuration Flags

| Setting | Default | Purpose |
| --- | --- | --- |
| `ACCOUNTS_BILLING_PERIOD_DAYS` | `30` | Length of each billing cycle. |
| `ACCOUNTS_CONCURRENCY_WINDOW_SECONDS` | `60` | How long an “active search” reservation lasts. |
| `INDEXING_SKIP_QDRANT_HEALTHCHECK` | `False` | Skips the Qdrant system check (useful in dev without Qdrant). |
| `DJANGO_USE_SQLITE` | `False` | Forces sqlite for local commands instead of Postgres (handy for makemigrations). |

Environment values should be set in `.env` (loaded via `config/settings.py`).

## 7. Extending the Module

1. **Credit Consumption** – Wire `spend_credits()` into AI search/generation endpoints once the request succeeds. Pass context (`reason`, `reference_type`, `reference_id`) for traceability.
2. **Add-ons & Promos** – Expose admin/API endpoints for `grant_top_up()` and promo redemptions. Record `source_identifier` (`stripe_charge`, `promo_code`) and metadata.
3. **Plan Management** – Implement upgrade/downgrade APIs that call `apply_plan_upgrade()` or `schedule_plan_downgrade()` after billing confirmation.
4. **Usage Stats** – Populate real counts for generated tests/favorites and extend `/stats/` to accept filters.
5. **Activity Feed** – Future work can translate `CreditUsageLog` plus other events into `/api/accounts/activity/`.

## 8. Testing Checklist

* Unit-test `subscriptions.py` credit math: rollover creation, priority ordering, proration, insufficient balance.
* Concurrency guard: verify Redis keys expire and that exceeding the plan limit raises `ConcurrencyLimitError`.
* API tests: `/auth/me/` returns seeded plan information, `/stats/` respects range parameters, logout stays idempotent.
* Migration tests: ensure `seed_plans` is idempotent and can be safely re-run.

With these pieces in place, the frontend profile page can render live plan and credit data, enforce concurrency limits, and guide users through plan upgrades and add-on purchases.
