# Threat model (summary)

| Threat | Mitigation | Residual risk |
|---|---|---|
| Prompt injection via email/doc/web/WhatsApp/file names | All such tool output is wrapped as untrusted data; system prompt forbids following it; policy lives in code, not the model; every send/delete/overwrite needs exact-action human approval | A fooled model can still *prepare* misleading drafts or ask misleading questions — read approvals |
| Model calls arbitrary OS APIs | Only registered tools, pydantic `extra=forbid`; no shell/script/install tool | Pixel-level control can click anything once you approve a control session |
| Secrets leakage | DPAPI store; redaction of keys/tokens in logs, audit, errors; secrets never returned by API; credential file types and browser stores blocked | A user can paste a secret into a prompt |
| Other websites hitting the local API | Loopback bind, per-launch token (URL fragment → sessionStorage), Host/Origin checks | Malware running as the same Windows user |
| Network attacker on the VPN | Pinned-fingerprint TLS, HMAC-signed requests with timestamp+nonce, 120 req/min limit, explicit pairing approval, revocation | Compromised paired PC can use the methods it is allowed (task submit, approvals, files into quarantine inbox) |
| Wrong-PC execution | Device selector, offline tasks rejected (never queued), each PC has its own approvals/credentials | User picks wrong target — banner and approval show device name |
| Duplicate sends after uncertain failure | Sent-ledger marked before send; no auto-retry of HIGH tools; digest check | Manual re-send by user |
| Stale/changed screen | Coordinate actions require a screenshot <120 s old and unchanged resolution | Window moved within that period |
| Privacy of screenshots | Vision off by default, explicit consent, pause toggle, exclusion list, excluded windows never captured | Excluded names are title-based |
