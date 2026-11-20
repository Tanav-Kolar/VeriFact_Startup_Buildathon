WITH deduped AS (
  SELECT
    event_id,
    source,
    source_id,
    query_id,
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
),
hn AS (
  SELECT
    CONCAT('hn:', COALESCE(JSON_VALUE(d.payload, '$.objectID'), JSON_VALUE(d.payload, '$.id'), d.source_id)) AS item_id,
    d.event_id AS event_id,
    'hn' AS platform,
    CASE
      WHEN JSON_VALUE(d.payload, '$.type') = 'comment'
        OR JSON_VALUE(d.payload, '$.comment_text') IS NOT NULL
        OR JSON_VALUE(d.payload, '$.parent_id') IS NOT NULL
      THEN 'comment'
      ELSE 'story'
    END AS kind,
    CONCAT(
      'https://news.ycombinator.com/item?id=',
      COALESCE(JSON_VALUE(d.payload, '$.objectID'), JSON_VALUE(d.payload, '$.id'), d.source_id)
    ) AS url,
    JSON_VALUE(d.payload, '$.author') AS author_ref,
    CAST(NULL AS INT64) AS author_followers,
    COALESCE(
      TIMESTAMP_SECONDS(SAFE_CAST(JSON_VALUE(d.payload, '$.created_at_i') AS INT64)),
      SAFE.TIMESTAMP(JSON_VALUE(d.payload, '$.created_at'))
    ) AS created_at,
    d.collected_at AS collected_at,
    CASE
      WHEN JSON_VALUE(d.payload, '$.type') = 'comment'
        OR JSON_VALUE(d.payload, '$.comment_text') IS NOT NULL
        OR JSON_VALUE(d.payload, '$.parent_id') IS NOT NULL
      THEN REGEXP_REPLACE(
        COALESCE(JSON_VALUE(d.payload, '$.comment_text'), JSON_VALUE(d.payload, '$.text'), ''),
        r'<[^>]+>',
        ''
      )
      ELSE TRIM(
        CONCAT(
          COALESCE(JSON_VALUE(d.payload, '$.title'), ''),
          CASE
            WHEN NULLIF(COALESCE(JSON_VALUE(d.payload, '$.story_text'), JSON_VALUE(d.payload, '$.text'), ''), '') IS NOT NULL
            THEN CONCAT(
              '\n\n',
              REGEXP_REPLACE(COALESCE(JSON_VALUE(d.payload, '$.story_text'), JSON_VALUE(d.payload, '$.text')), r'<[^>]+>', '')
            )
            ELSE ''
          END
        )
      )
    END AS text,
    CAST(NULL AS STRING) AS lang,
    IF(
      JSON_VALUE(d.payload, '$.parent_id') IS NOT NULL,
      CONCAT('hn:', JSON_VALUE(d.payload, '$.parent_id')),
      CAST(NULL AS STRING)
    ) AS parent_id,
    CAST(NULL AS STRING) AS quote_of,
    IF(
      NULLIF(JSON_VALUE(d.payload, '$.url'), '') IS NOT NULL,
      [JSON_VALUE(d.payload, '$.url')],
      ARRAY<STRING>[]
    ) AS links,
    STRUCT(
      CAST(NULL AS INT64) AS likes,
      CAST(NULL AS INT64) AS reposts,
      SAFE_CAST(JSON_VALUE(d.payload, '$.num_comments') AS INT64) AS replies,
      CAST(NULL AS INT64) AS views,
      SAFE_CAST(JSON_VALUE(d.payload, '$.points') AS INT64) AS score
    ) AS engagement,
    SAFE_DIVIDE(
      CAST(
        TIMESTAMP_DIFF(
          COALESCE(
            TIMESTAMP_SECONDS(SAFE_CAST(JSON_VALUE(d.payload, '$.created_at_i') AS INT64)),
            SAFE.TIMESTAMP(JSON_VALUE(d.payload, '$.created_at'))
          ),
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
  WHERE d.source = 'hn'
),
yt_channels AS (
  SELECT
    d.event_id AS event_id,
    JSON_VALUE(d.payload, '$.id') AS channel_id,
    SAFE_CAST(JSON_VALUE(d.payload, '$.statistics.subscriberCount') AS INT64) AS subscriber_count
  FROM deduped d
  WHERE d.source = 'youtube'
    AND (d.query_id = 'channel' OR JSON_VALUE(d.payload, '$.kind') = 'youtube#channel')
),
youtube AS (
  SELECT
    CONCAT('yt:', JSON_VALUE(d.payload, '$.id')) AS item_id,
    d.event_id AS event_id,
    'youtube' AS platform,
    'video' AS kind,
    CONCAT('https://www.youtube.com/watch?v=', JSON_VALUE(d.payload, '$.id')) AS url,
    JSON_VALUE(d.payload, '$.snippet.channelId') AS author_ref,
    c.subscriber_count AS author_followers,
    TIMESTAMP(JSON_VALUE(d.payload, '$.snippet.publishedAt')) AS created_at,
    d.collected_at AS collected_at,
    TRIM(
      CONCAT(
        COALESCE(JSON_VALUE(d.payload, '$.snippet.title'), ''),
        IF(
          NULLIF(JSON_VALUE(d.payload, '$.snippet.description'), '') IS NOT NULL,
          CONCAT('\n\n', JSON_VALUE(d.payload, '$.snippet.description')),
          ''
        )
      )
    ) AS text,
    COALESCE(
      JSON_VALUE(d.payload, '$.snippet.defaultAudioLanguage'),
      JSON_VALUE(d.payload, '$.snippet.defaultLanguage')
    ) AS lang,
    CAST(NULL AS STRING) AS parent_id,
    CAST(NULL AS STRING) AS quote_of,
    ARRAY<STRING>[] AS links,
    STRUCT(
      SAFE_CAST(JSON_VALUE(d.payload, '$.statistics.likeCount') AS INT64) AS likes,
      CAST(NULL AS INT64) AS reposts,
      SAFE_CAST(JSON_VALUE(d.payload, '$.statistics.commentCount') AS INT64) AS replies,
      SAFE_CAST(JSON_VALUE(d.payload, '$.statistics.viewCount') AS INT64) AS views,
      CAST(NULL AS INT64) AS score
    ) AS engagement,
    SAFE_DIVIDE(
      CAST(
        TIMESTAMP_DIFF(
          TIMESTAMP(JSON_VALUE(d.payload, '$.snippet.publishedAt')),
          t.t0,
          SECOND
        ) AS FLOAT64
      ),
      3600.0
    ) AS hours_from_t0,
    d.captured_by AS captured_by,
    FALSE AS time_approximate
  FROM deduped d
  LEFT JOIN yt_channels c
    ON d.event_id = c.event_id
    AND JSON_VALUE(d.payload, '$.snippet.channelId') = c.channel_id
  LEFT JOIN `${GCP_PROJECT}.${BQ_DATASET}.event_t0` t
    ON d.event_id = t.event_id
  WHERE d.source = 'youtube'
    AND (d.query_id = 'video' OR JSON_VALUE(d.payload, '$.kind') = 'youtube#video')

  UNION ALL

  SELECT
    CONCAT('yt:c:', JSON_VALUE(d.payload, '$.id')) AS item_id,
    d.event_id AS event_id,
    'youtube' AS platform,
    'comment' AS kind,
    CONCAT(
      'https://www.youtube.com/watch?v=',
      JSON_VALUE(d.payload, '$.snippet.videoId'),
      '&lc=',
      JSON_VALUE(d.payload, '$.id')
    ) AS url,
    JSON_VALUE(d.payload, '$.snippet.authorChannelId.value') AS author_ref,
    CAST(NULL AS INT64) AS author_followers,
    TIMESTAMP(JSON_VALUE(d.payload, '$.snippet.publishedAt')) AS created_at,
    d.collected_at AS collected_at,
    COALESCE(
      JSON_VALUE(d.payload, '$.snippet.textOriginal'),
      JSON_VALUE(d.payload, '$.snippet.textDisplay')
    ) AS text,
    CAST(NULL AS STRING) AS lang,
    CASE
      WHEN JSON_VALUE(d.payload, '$.snippet.parentId') IS NOT NULL
      THEN CONCAT('yt:c:', JSON_VALUE(d.payload, '$.snippet.parentId'))
      WHEN JSON_VALUE(d.payload, '$.snippet.videoId') IS NOT NULL
      THEN CONCAT('yt:', JSON_VALUE(d.payload, '$.snippet.videoId'))
      ELSE CAST(NULL AS STRING)
    END AS parent_id,
    CAST(NULL AS STRING) AS quote_of,
    ARRAY<STRING>[] AS links,
    STRUCT(
      SAFE_CAST(JSON_VALUE(d.payload, '$.snippet.likeCount') AS INT64) AS likes,
      CAST(NULL AS INT64) AS reposts,
      CAST(NULL AS INT64) AS replies,
      CAST(NULL AS INT64) AS views,
      CAST(NULL AS INT64) AS score
    ) AS engagement,
    SAFE_DIVIDE(
      CAST(
        TIMESTAMP_DIFF(
          TIMESTAMP(JSON_VALUE(d.payload, '$.snippet.publishedAt')),
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
  WHERE d.source = 'youtube'
    AND (
      STARTS_WITH(COALESCE(d.query_id, ''), 'comments:')
      OR JSON_VALUE(d.payload, '$.kind') = 'youtube#comment'
    )
),
gdelt AS (
  SELECT
    CONCAT('gdelt:', d.source_id) AS item_id,
    d.event_id AS event_id,
    'news' AS platform,
    'article' AS kind,
    JSON_VALUE(d.payload, '$.url') AS url,
    JSON_VALUE(d.payload, '$.domain') AS author_ref,
    CAST(NULL AS INT64) AS author_followers,
    COALESCE(
      SAFE.TIMESTAMP(JSON_VALUE(f.payload, '$.data.metadata["article:published_time"]')),
      SAFE.PARSE_TIMESTAMP('%Y%m%dT%H%M%SZ', JSON_VALUE(d.payload, '$.seendate')),
      SAFE.PARSE_TIMESTAMP('%Y%m%d%H%M%S', JSON_VALUE(d.payload, '$.seendate'))
    ) AS created_at,
    d.collected_at AS collected_at,
    CASE
      WHEN JSON_VALUE(f.payload, '$.data.markdown') IS NOT NULL
      THEN CONCAT(JSON_VALUE(d.payload, '$.title'), '\n\n', JSON_VALUE(f.payload, '$.data.markdown'))
      ELSE JSON_VALUE(d.payload, '$.title')
    END AS text,
    COALESCE(
      JSON_VALUE(f.payload, '$.data.metadata.language'),
      JSON_VALUE(d.payload, '$.language')
    ) AS lang,
    CAST(NULL AS STRING) AS parent_id,
    CAST(NULL AS STRING) AS quote_of,
    ARRAY<STRING>[] AS links,
    STRUCT(
      CAST(NULL AS INT64) AS likes,
      CAST(NULL AS INT64) AS reposts,
      CAST(NULL AS INT64) AS replies,
      CAST(NULL AS INT64) AS views,
      CAST(NULL AS INT64) AS score
    ) AS engagement,
    SAFE_DIVIDE(
      CAST(
        TIMESTAMP_DIFF(
          COALESCE(
            SAFE.TIMESTAMP(JSON_VALUE(f.payload, '$.data.metadata["article:published_time"]')),
            SAFE.PARSE_TIMESTAMP('%Y%m%dT%H%M%SZ', JSON_VALUE(d.payload, '$.seendate')),
            SAFE.PARSE_TIMESTAMP('%Y%m%d%H%M%S', JSON_VALUE(d.payload, '$.seendate'))
          ),
          t.t0,
          SECOND
        ) AS FLOAT64
      ),
      3600.0
    ) AS hours_from_t0,
    d.captured_by AS captured_by,
    CASE
      WHEN SAFE.TIMESTAMP(JSON_VALUE(f.payload, '$.data.metadata["article:published_time"]')) IS NOT NULL THEN FALSE
      ELSE TRUE
    END AS time_approximate
  FROM deduped d
  LEFT JOIN `${GCP_PROJECT}.${BQ_DATASET}.event_t0` t
    ON d.event_id = t.event_id
  LEFT JOIN (
    SELECT event_id, source_id, payload
    FROM deduped
    WHERE source = 'firecrawl' AND query_id = 'article'
  ) f
    ON d.event_id = f.event_id AND d.source_id = f.source_id
  WHERE d.source = 'gdelt'
),
web AS (
  SELECT
    CONCAT('web:', d.source_id) AS item_id,
    d.event_id AS event_id,
    'web' AS platform,
    'official_page' AS kind,
    COALESCE(
      JSON_VALUE(d.payload, '$.data.metadata.sourceURL'),
      JSON_VALUE(d.payload, '$.data.metadata.url')
    ) AS url,
    CAST(NULL AS STRING) AS author_ref,
    CAST(NULL AS INT64) AS author_followers,
    d.collected_at AS created_at,
    d.collected_at AS collected_at,
    JSON_VALUE(d.payload, '$.data.markdown') AS text,
    JSON_VALUE(d.payload, '$.data.metadata.language') AS lang,
    CAST(NULL AS STRING) AS parent_id,
    CAST(NULL AS STRING) AS quote_of,
    ARRAY<STRING>[] AS links,
    STRUCT(
      CAST(NULL AS INT64) AS likes,
      CAST(NULL AS INT64) AS reposts,
      CAST(NULL AS INT64) AS replies,
      CAST(NULL AS INT64) AS views,
      CAST(NULL AS INT64) AS score
    ) AS engagement,
    SAFE_DIVIDE(
      CAST(
        TIMESTAMP_DIFF(
          d.collected_at,
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
  WHERE d.source = 'firecrawl' AND d.query_id = 'official'
)
SELECT * FROM x
UNION ALL
SELECT * FROM hn
UNION ALL
SELECT * FROM youtube
UNION ALL
SELECT * FROM gdelt
UNION ALL
SELECT * FROM web
