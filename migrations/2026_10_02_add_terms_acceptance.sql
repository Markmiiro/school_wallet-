-- ================================================
-- Terms and privacy acceptance, recorded per user.
--
-- Adds to `users`:
--   terms_version      which version of the terms the user accepted
--   terms_accepted_at  when they accepted it (UTC)
--
-- Both are NULL for every existing account, which the application
-- reads as "has not accepted": a parent is asked once at next login.
--
-- RUN THIS BY HAND, BEFORE DEPLOYING THE CODE THAT USES IT. The User
-- model selects these columns on every query, so the new code cannot
-- log anyone in until they exist. The old code ignores them, so
-- running this first is safe.
--
-- Additive only: no data is changed or removed. Safe to run twice.
-- PostgreSQL.
-- ================================================

BEGIN;

ALTER TABLE users ADD COLUMN IF NOT EXISTS terms_version     VARCHAR;
ALTER TABLE users ADD COLUMN IF NOT EXISTS terms_accepted_at TIMESTAMP;

COMMIT;

-- To undo (only once no code reads the columns):
--   ALTER TABLE users DROP COLUMN terms_accepted_at;
--   ALTER TABLE users DROP COLUMN terms_version;
