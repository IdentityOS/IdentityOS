"""
core/interop — generic external interoperability protocol drivers.

This package holds transport + protocol primitives that let IdentityOS
identities talk to external environments over well-known agent protocols
without coupling the runtime to any single vendor:

    http    — shared HTTP transport (user-agent disclosure, timeouts, size caps,
              JSON/SSE body parsing)
    mcp     — Model Context Protocol: Streamable HTTP client, tool discovery,
              invocation, and capability-agnostic tool-risk classification
    a2a     — Agent-to-Agent: agent-card discovery, capability inspection,
              message send / task receive

Protocol drivers are stateless and know nothing about identities or storage.
Capability wrappers (core/capabilities/mcp, core/capabilities/a2a,
core/capabilities/culture_commons) bind them to the IdentityOS capability
model.
"""