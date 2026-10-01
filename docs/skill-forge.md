# Skill Forge

Skill Forge is the authoritative generation stage for capabilities that are
not already present in the marketplace. It extends the existing Prometheus to
Executive acquisition path; it is not a second planner.

## Lifecycle

```text
explicit missing ability
-> Prometheus need
-> durable Executive task
-> independent black-box cases
-> candidate source
-> static permission/dependency audit
-> clean subprocess execution
-> content-digested .idcap package
-> publish/install/activate
-> invoke/persist/reload/reuse
-> retry original request
```

The test designer runs before the implementation author and never receives
candidate source. A candidate must pass at least two observable examples. A
success string, import, manifest, or class shape is not sufficient evidence.

The isolated runner receives only `PATH`, the IdentityOS import path, a fixed
hash seed, and its temporary directory. Provider keys and other parent-process
secrets are not inherited. Static analysis rejects dynamic evaluation,
dangerous reflection/import mechanisms, undeclared risk scopes, and undeclared
third-party dependencies before the subprocess starts.

The same audit runs again at every source-loading boundary, using permissions
and dependencies from the content-digested manifest. Generated code may import
only the public capability API beneath `core`; imports of other runtime internals
are rejected. The dynamic loader and fixed-argv subprocess invocation are narrow,
reviewed code-execution boundaries and are annotated as such for static analysis.

## Portable artifact

An `.idcap` file contains exactly:

- `capability.py`
- `manifest.json`
- `acceptance.json`
- `attestation.json`

The attestation binds the other three files with SHA-256. Installation checks
that digest, reruns the behavioral cases, persists the tested package for the
target identity, and then installs it. A fresh `CapabilityRegistry` loads the
identity-specific verified source, so reuse does not depend on stale in-memory
registration.

## Authorization boundary

Forge-time execution is bounded by `allowed_permissions` and
`allowed_dependencies`. A model cannot authorize either. Runtime installation
does not grant sensitive skill permissions; the ordinary capability permission
gateway remains authoritative.

The subprocess boundary and static audit reduce risk but are not a claim of
kernel-level sandboxing. Production deployments should run the forge worker in
their existing container or VM sandbox when generated code is allowed to use
filesystem, network, process, or device scopes.
