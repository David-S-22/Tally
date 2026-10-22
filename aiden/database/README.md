# Anomalies Database

## Overview

The anomalies database is a Flask and SQLAlchemy application that exposes a
REST API for storing and managing anomaly records. It persists data in SQLite.
Each anomaly references a transaction by ID, and the unique constraint on
`anomalies.transaction_id` allows each transaction to have at most one anomaly.
An optional confidence score is stored from `0` to `1`; seeded scores are higher
for findings supported by stronger evidence and confirmed as accurate, and
lower for findings dismissed as false positives. Each anomaly also stores
`sources`: a JSON list of the RAG reference-document filenames the agent used to
ground its finding (an empty list when none were retrieved).
When a finding was created by the shared Multi-Agent Server, `workflow_id`
stores the corresponding workflow reference; it is null for legacy findings.

The `transactions` table and `anomalies` table are stored in separate databases. Therefore, the relationship between `anomalies.transaction_id` and `transactions.id` is a logical cross-database reference rather than an enforced database foreign key. The `UNIQUE` constraint on `anomalies.transaction_id` ensures that each transaction can have zero or one anomaly.

## Startup reconciliation

Because the reference to `transactions.id` is not an enforced foreign key,
anomalies can become orphaned if a transaction is deleted while the anomalies
database is unavailable. A background reconciliation pass starts with the
container, so the API can serve requests immediately while the pass proceeds:

1. It polls the transactions database (`GET {TRANSACTIONS_DB_URL}/transactions`),
   retrying until the service is reachable.
2. It inserts any missing demo findings whose transaction IDs exist in the
   returned set, without overwriting existing anomalies or review decisions.
   The seed includes 17 findings with a mix of confirmed, dismissed, and
   unreviewed user decisions, each with a confidence score between `0` and `1`.
3. It deletes any anomaly whose `transaction_id` is not present in the returned
   set of transactions.

This requires the `transactions-db` container to be running, so `anomalies-db`
declares a `depends_on` relationship on it in `docker-compose.yml`. The pass is
best-effort: if the transactions database cannot be reached within the retry
budget, reconciliation is skipped and logged rather than crashing the container.

The behaviour is configured through environment variables, all of which are
required:

| Variable | Purpose |
| --- | --- |
| `TRANSACTIONS_DB_URL` | Base URL of the transactions database. |
| `TRANSACTIONS_TIMEOUT_SECONDS` | Per-request timeout when polling transactions. |
| `RECONCILE_MAX_RETRIES` | Number of polling attempts before giving up. |
| `RECONCILE_RETRY_DELAY_SECONDS` | Delay between polling attempts. |

The Compose deployment allows up to 60 seconds per transactions request and
waits 5 seconds between retries.

## Routes

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/` | Returns the database health response. |
| `GET` | `/anomalies/` | Returns all anomaly records. |
| `POST` | `/anomalies/` | Creates an anomaly record. |
| `GET` | `/anomalies/<id>` | Returns one anomaly by ID. |
| `GET` | `/anomalies/by-transaction/<id>` | Returns the anomaly for a transaction. |
| `PATCH` | `/anomalies/<id>` | Updates the user's confirmation status. |
| `DELETE` | `/anomalies/<id>` | Deletes an anomaly by ID. |
| `DELETE` | `/anomalies/by-transaction/<id>` | Deletes the anomaly for a transaction. |

## Conceptual ERD

```mermaid
erDiagram
    TRANSACTIONS ||--o| ANOMALIES : "may have"

    TRANSACTIONS {
        int id
        date date
        string merchant
        string description
        decimal amount
        int category_id
        datetime created_at
        datetime updated_at
    }

    ANOMALIES {
        int id
        int transaction_id
        string agent_reason_suspected
        boolean is_confirmed_by_user
        float confidence "0 to 1, nullable"
        json sources "RAG source filenames"
        string workflow_id "nullable shared workflow reference"
    }
```

## Logical ERD

```mermaid
erDiagram
    TRANSACTIONS ||--o| ANOMALIES : "has at most one"

    TRANSACTIONS {
        int id PK
        datetime date
        string merchant
        string description
        decimal amount
        int category_id
        datetime created_at
        datetime updated_at
    }

    ANOMALIES {
        int id PK
        int transaction_id UK
        string agent_reason_suspected
        boolean is_confirmed_by_user
        float confidence "0 to 1, nullable"
        json sources "RAG source filenames"
        string workflow_id "nullable shared workflow reference"
    }
```

## Physical ERD

```mermaid
erDiagram
    TRANSACTIONS ||--o| ANOMALIES : "transaction_id reference"

    TRANSACTIONS {
        INTEGER id PK "NOT NULL"
        DATETIME date "NOT NULL"
        VARCHAR_200 merchant "NOT NULL"
        VARCHAR_500 description "NOT NULL"
        NUMERIC_12_2 amount "NOT NULL"
        INTEGER category_id "NOT NULL"
        DATETIME created_at "NOT NULL"
        DATETIME updated_at "NOT NULL"
    }

    ANOMALIES {
        INTEGER id PK "NOT NULL"
        INTEGER transaction_id UK "NOT NULL"
        VARCHAR agent_reason_suspected "NOT NULL"
        BOOLEAN is_confirmed_by_user "NULLABLE"
        FLOAT confidence "NULLABLE, 0 <= confidence <= 1"
        JSON sources "NOT NULL, defaults to []"
        VARCHAR workflow_id "NULLABLE"
    }
```
