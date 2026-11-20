# Spec 03: Shared BigQuery writer and cursor store

## 1. Goal
Give every collector one tested way to append rows to `raw_items` and to keep its cursor and X read count in Firestore.

## 2. Inputs and outputs
**Inputs:** `steering_docs/data-model.md` (`raw_items`, Firestore `collector_cursors`), spec 01's `verifact_core`.

**Outputs**
- `verifact_core/bq.py`: `write_raw_items(rows: list[RawItem], *, run_id: str) -> WriteResult`
- `verifact_core/models.py`: Pydantic `RawItem` (fields of `raw_items`), `WriteResult` (`written: int, rejected: list[Rejection]`)
- `verifact_core/cursors.py`: `get_cursor(event_id, source, query_id) -> Cursor | None`, `set_cursor(...)`, `add_reads(event_id, source, n) -> int` (returns the new total), `get_reads(event_id, source) -> int`
- `tests/test_bq_writer.py`, `tests/test_cursors.py`

## 3. Steps
1. `RawItem` validates: `event_id`, `source`, `source_id` non-empty; `source` in the data-model enum; `collected_at` tz-aware UTC; `payload` is a dict, stored unmodified.
2. `write_raw_items` inserts in batches of at most 500 rows (BigQuery Storage Write API or `insert_rows_json`). Rows that fail are not retried forever: up to 3 attempts with exponential backoff, then each one is logged as a structured `bq_insert_error` (`source`, `source_id`, `reason`) and returned in `rejected`.
3. The writer never issues UPDATE or DELETE, and never modifies `payload`.
4. Cursor documents live at `collector_cursors/{event_id}__{source}__{query_id}` with `cursor`, `updated_at` and optional `step_done: bool`.
5. Read counts live at `collector_cursors/{event_id}__{source}__reads` with `reads_total`. `add_reads` uses a Firestore transaction or `Increment`, so concurrent runs never lose counts. Nothing resets `reads_total`.
6. Project, dataset and database names come from env (`GCP_PROJECT`, `BQ_DATASET`, `FIRESTORE_DATABASE` default `(default)`).
7. Tests use mocks or the Firestore emulator. They make no calls to real GCP.

## 4. Acceptance criteria
1. Writing 1,200 valid rows makes 3 insert calls, and `written == 1200`.
2. A row the backend rejects appears in `rejected` and in a `bq_insert_error` log line with its reason, and the other rows are still written.
3. `RawItem` rejects a naive datetime, an unknown `source` and an empty `source_id`.
4. The payload stored equals the payload passed in, byte for byte after JSON round-trip.
5. Two concurrent `add_reads(..., 50)` calls starting from 100 end at 200.
6. `get_cursor` returns `None` for an unknown key, and `set_cursor` then `get_cursor` round-trips.
7. `grep -rniE "UPDATE|DELETE" verifact_core/bq.py` finds no SQL statement.
8. No test touches real GCP resources.

## 5. Out of scope
Any collector logic. Pub/Sub (not used). The `items` view.

## 6. Open questions
- None blocking. The cursor store is Firestore, as recommended in data-model.md.
