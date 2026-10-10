-- Run once after backing up the database. Existing rows keep their owner user_id.
BEGIN;
ALTER TABLE publish.operators ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE publish.edge_native_deployments ALTER COLUMN user_id DROP DEFAULT;

-- Do not silently assign legacy NULL rows to a made-up user.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM publish.operators WHERE user_id IS NULL OR btrim(user_id) = '') THEN
        RAISE EXCEPTION 'publish.operators has missing user_id; migrate these rows explicitly';
    END IF;
    IF EXISTS (
        SELECT 1 FROM publish.edge_native_deployments
        WHERE user_id IS NULL OR btrim(user_id) = ''
    ) THEN
        RAISE EXCEPTION 'publish.edge_native_deployments has missing user_id; migrate these rows explicitly';
    END IF;
END
$$;

ALTER TABLE publish.operators ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE publish.edge_native_deployments ALTER COLUMN user_id SET NOT NULL;
COMMIT;
