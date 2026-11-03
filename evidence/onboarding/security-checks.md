# Security checks

Verified on 2026-11-03:

- Dashboard lifecycle bound to `127.0.0.1`; a public bind was rejected.
- Requests with a non-loopback Host or cross-site Origin were rejected.
- Browser onboarding mutations required a matching SameSite cookie and CSRF header.
- Security headers deny framing, MIME sniffing, camera, microphone, and geolocation.
- API keys were stored outside JSON configuration in mode-0600 secret files.
- Public onboarding state and provider responses omitted credential values and handles.
- Export used a synthetic secret canary and proved it absent before import.
- The clean-run child environment removed credential-shaped variables by default.
- Evidence logs and argv were passed through exact-value and credential-shape redaction.
- The real Ollama run used no credential.
- Uninstall preserved identity data by default; purge required two explicit flags.
- Installer activation occurred only after the new isolated environment exposed a valid `idos` command.
- A failed candidate installer did not replace the active release.

Not claimed:

- The playground is not an authenticated remote multi-user service. It is
  intentionally local-only; remote use requires a separately secured proxy.
- macOS, WSL2, native Windows, machine restart, and hostile local-user isolation
  were not tested.
- No package signature or release attestation exists because no release was
  published in this work.
