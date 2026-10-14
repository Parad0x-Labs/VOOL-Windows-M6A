# Historical migration fixtures

`legacy_7ff50e8d.sql` is the exact `SCHEMA_SQL` string extracted with
`ast.literal_eval` from `storage/migrations.py` at commit
`7ff50e8d42ed13bbe9fc888ac649d6d942731202` (Git blob
`c9b7adee09c26dce8a73ba1c164652b131089cc7`). The historical module was not imported
or executed. This fixture contains schema definitions, not database contents or user rows.

The UTF-8 digest of the extracted string is
`e2ab521f62ffbeae46b8dd367f606025fcc0d7395bf7471c62df084e2a5f52a3`.
The loader normalizes checkout line endings through text decoding before checking
that digest. No columns, indexes, defaults or schema statements were synthesized,
removed or generated from the current migration code.

Keeping this input in the test tree makes the original upgrade regression
reproducible in shallow clones and source archives without importing historical
Git objects. Changing a fixture requires independently recovered source evidence;
updating its pin to match an invented old schema is not a valid repair.
