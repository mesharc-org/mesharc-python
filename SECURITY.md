# Security policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x    | yes       |
| older   | no — upgrade |

## Reporting a vulnerability

Write to **security@mesharc.dev**. Please do not open a public issue for a security problem.

- We acknowledge a report within two working days and tell you what we found and when it will be fixed.
- Give us a reasonable time to fix the problem before you publish anything about it. We credit reporters who want to be credited.
- Test only against accounts you own; do not read, change or delete anyone else's data, and do not run denial-of-service or automated scanning against the production API.

## Scope

This policy covers `pip install mesharc` (this repository). A problem in the MeshArc service itself — the app at mesharc.dev or the API at api.mesharc.dev — goes to the same address; the service's own policy is at [mesharc.dev/legal/security](https://mesharc.dev/legal/security).

## What this client does with your data

It talks to one host, the API base URL, and to nothing else. The key travels only as a bearer header over HTTPS. Nothing is written to disk and no telemetry is sent. What MeshArc keeps, and for how long, is in the [privacy policy](https://mesharc.dev/legal/privacy).
