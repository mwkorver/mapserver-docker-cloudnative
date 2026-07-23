# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report vulnerabilities privately through GitHub's built-in tool:
**Security → Report a vulnerability**
(https://github.com/mwkorver/mapserver-docker-cloudnative/security/advisories/new).
This keeps the report confidential until a fix is available.

Please include:

- a description of the issue and its impact,
- steps to reproduce (or a proof of concept),
- affected component (nginx proxy, SigV4 signer, MapServer/GDAL, admin, CDK/IAM) and version/commit.

You can expect an initial acknowledgement within a few business days.

## Scope notes

This project runs a server-side MapServer + GDAL stack behind nginx that reads
COGs from Amazon S3, with an nginx range-cache and a SigV4 request signer in
front of GDAL. Findings that are especially in scope:

- the SigV4 signer (`etc/s3_sigv4_proxy.py`) signing requests for buckets or
  objects outside the intended prefix,
- credential and request handling in the nginx proxy / range-cache layer,
- the admin scan interface (`admin/`) exposing mutation or index-scan actions
  without the expected access control,
- the IAM roles and policies under `iam/` and the CDK stack under `cdk/`
  (task vs. execution role least-privilege).

Out of scope: costs incurred by serving imagery from requester-pays buckets
(this is expected behavior), and the local-mode learning setup, which the
README already flags as not a performance-measurement or hardened environment.
