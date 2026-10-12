# Security Policy

## Supported versions

Only the latest commit on `main` is supported.

## Reporting a vulnerability

nimbus-db is an educational single-process database engine. If you find a
security-relevant bug (data corruption, crash, escape from expected
behavior), please open a GitHub issue with a minimal reproduction. For
anything sensitive, use a private security advisory instead of a public
issue.

## Scope

nimbus-db does not implement authentication, networking, or multi-tenant
isolation — it opens files passed to `Database(path)` in the current process.
Running untrusted SQL against it is **not** a supported use case (the SQL
surface is not sandboxed, by design).
