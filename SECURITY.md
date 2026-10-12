# Security policy

## Supported versions
Security fixes go into the latest release. Please update before reporting.

## Reporting a vulnerability
**Please do not open a public issue.** Use GitHub's private reporting:
**Security → Report a vulnerability** on this repository
([direct link](../../security/advisories/new)). You will get a reply within 7 days.

Include what you found, how to reproduce it, and the impact you expect.

## What is in scope
Grain Search runs a local HTTP service so the app can talk to the search engine. It is designed to:
- listen on `127.0.0.1` only, never on the network;
- require a random per-session token on every request except `/health`;
- accept browser requests only from the app's own origin (CORS);
- serve and open **only files that are in the index**, never arbitrary paths;
- never send file contents or queries anywhere. The only network use is the one-time model
  download and the update check.

Anything that breaks one of these promises is a security issue we want to hear about. So are
problems with the installer, the updater (signature checks) or the release artifacts.
