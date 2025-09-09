# Letterboxd Activity Watcher

Daily watcher for a Letterboxd RSS feed. Sends a clean HTML email (poster, watched/rewatched, rating, review) and writes every item to Snowflake.

## How to use
1. Add repo **secrets**:
   - Email: `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `EMAIL_TO`
   - Feed: `LBX_FEED_URL` (e.g. `https://letterboxd.com/<user>/rss/`), `LBX_USER`
   - Snowflake: `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD`, `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA`
2. Workflow: `.github/workflows/watch.yml` (runs on schedule + manual trigger).
3. Deps: `requirements.txt` (feedparser, bs4, snowflake-connector-python).

## What gets stored (Snowflake)
Table `LETTERBOXD_ACTIVITY` (upsert by `guid`): film title/year, kind (watched/rewatch/list), rating **stars** + **numeric**, review flag/text, published_at, watched_date, poster_url, action_summary, fetched_at.
