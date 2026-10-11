-- Model invocations (design 05 §3.2): one row per call to a model, including cache
-- hits. Facts, like decisions: written outside commit, never updated.
CREATE TABLE model_invocations (
    invocation_id VARCHAR PRIMARY KEY,
    provider      VARCHAR NOT NULL,
    model         VARCHAR NOT NULL,
    model_build   VARCHAR,
    prompt_ref    VARCHAR NOT NULL,
    settings      JSON NOT NULL,
    cache_key     VARCHAR NOT NULL,
    payload       JSON NOT NULL,         -- as sent, after privacy filtering
    response      JSON,
    output        JSON,
    status        VARCHAR NOT NULL,      -- ok | invalid | refused | error | cached
    error         VARCHAR,
    attempts      INTEGER NOT NULL,
    cached_from   VARCHAR,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    latency_ms    INTEGER,
    cost_usd      DOUBLE,
    run_id        VARCHAR,
    stage_id      VARCHAR,
    created_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX model_invocations_cache ON model_invocations (cache_key);
CREATE INDEX model_invocations_run ON model_invocations (run_id);
