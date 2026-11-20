WITH deduped AS (
  SELECT
    event_id,
    source,
    source_id,
    captured_by,
    collected_at,
    payload
  FROM `${GCP_PROJECT}.${BQ_DATASET}.raw_items`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY event_id, source, source_id
    ORDER BY collected_at ASC
  ) = 1
),
x AS (
  SELECT
    CONCAT('x:', JSON_VALUE(d.payload, '$.id')) AS item_id,
    d.event_id AS event_id,
    'x' AS platform,
    CASE
      WHEN EXISTS (
        SELECT 1
        FROM UNNEST(JSON_QUERY_ARRAY(d.payload, '$.referenced_tweets')) AS ref
        WHERE JSON_VALUE(ref, '$.type') = 'replied_to'
      ) THEN 'reply'
      WHEN EXISTS (
        SELECT 1
        FROM UNNEST(JSON_QUERY_ARRAY(d.payload, '$.referenced_tweets')) AS ref
        WHERE JSON_VALUE(ref, '$.type') = 'quoted'
      ) THEN 'quote'
      ELSE 'post'
    END AS kind,
    CONCAT('https://x.com/i/status/', JSON_VALUE(d.payload, '$.id')) AS url,
    JSON_VALUE(d.payload, '$.author_id') AS author_ref,
    CAST(NULL AS INT64) AS author_followers,
    TIMESTAMP(JSON_VALUE(d.payload, '$.created_at')) AS created_at,
    d.collected_at AS collected_at,
    JSON_VALUE(d.payload, '$.text') AS text,
    JSON_VALUE(d.payload, '$.lang') AS lang,
    (
      SELECT CONCAT('x:', JSON_VALUE(ref, '$.id'))
      FROM UNNEST(JSON_QUERY_ARRAY(d.payload, '$.referenced_tweets')) AS ref
      WHERE JSON_VALUE(ref, '$.type') = 'replied_to'
      LIMIT 1
    ) AS parent_id,
    (
      SELECT CONCAT('x:', JSON_VALUE(ref, '$.id'))
      FROM UNNEST(JSON_QUERY_ARRAY(d.payload, '$.referenced_tweets')) AS ref
      WHERE JSON_VALUE(ref, '$.type') = 'quoted'
      LIMIT 1
    ) AS quote_of,
    ARRAY(
      SELECT JSON_VALUE(u, '$.expanded_url')
      FROM UNNEST(JSON_QUERY_ARRAY(d.payload, '$.entities.urls')) AS u
      WHERE JSON_VALUE(u, '$.expanded_url') IS NOT NULL
    ) AS links,
    STRUCT(
      COALESCE(CAST(JSON_VALUE(d.payload, '$.public_metrics.like_count') AS INT64), 0) AS likes,
      COALESCE(CAST(JSON_VALUE(d.payload, '$.public_metrics.retweet_count') AS INT64), 0)
        + COALESCE(CAST(JSON_VALUE(d.payload, '$.public_metrics.quote_count') AS INT64), 0) AS reposts,
      COALESCE(CAST(JSON_VALUE(d.payload, '$.public_metrics.reply_count') AS INT64), 0) AS replies,
      CAST(JSON_VALUE(d.payload, '$.public_metrics.impression_count') AS INT64) AS views,
      CAST(NULL AS INT64) AS score
    ) AS engagement,
    SAFE_DIVIDE(
      CAST(
        TIMESTAMP_DIFF(
          TIMESTAMP(JSON_VALUE(d.payload, '$.created_at')),
          t.t0,
          SECOND
        ) AS FLOAT64
      ),
      3600.0
    ) AS hours_from_t0,
    d.captured_by AS captured_by,
    FALSE AS time_approximate
  FROM deduped d
  LEFT JOIN `${GCP_PROJECT}.${BQ_DATASET}.event_t0` t
    ON d.event_id = t.event_id
  WHERE d.source = 'x'
)
SELECT * FROM x
-- Future source CTEs (hn, youtube, gdelt, firecrawl, manual) are added via UNION ALL in specs 04-05.
