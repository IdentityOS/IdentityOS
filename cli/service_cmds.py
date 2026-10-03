"""Trusted host CLI for local service identities. No remote impersonation endpoint."""

import json
from pathlib import Path


def add_service_parser(parser):
    parser.add_argument("--store", default=".identity_store")
    parser.add_argument("--identity", default="engineer")
    sub = parser.add_subparsers(dest="service_command", required=True)
    sub.add_parser("init-engineer")
    for name in [
        "status",
        "specifications",
        "directory",
        "jobs",
        "messages",
        "relationships",
        "reputation",
        "balance",
        "ledger",
        "provenance",
        "artifacts",
        "recover",
        "tick",
    ]:
        sub.add_parser(name)
    serve = sub.add_parser("serve")
    serve.add_argument("--interval", type=float, default=5.0)
    sub.add_parser("requester-tick")
    show = sub.add_parser("show")
    show.add_argument("job")
    proposal = sub.add_parser("propose")
    proposal.add_argument("provider")
    proposal.add_argument("contract", type=Path)
    respond = sub.add_parser("respond")
    respond.add_argument("reference")
    respond.add_argument("revision", type=int)
    respond.add_argument("decision", choices=["ACCEPT", "COUNTERPROPOSE", "REQUEST_CLARIFICATION", "DECLINE"])
    respond.add_argument("--contract", type=Path)
    start = sub.add_parser("start-job")
    start.add_argument("reference")
    req = sub.add_parser("request")
    req.add_argument("provider")
    req.add_argument("contract", type=Path)
    for name in ["quote", "accept-quote", "accept-delivery", "cancel", "rework"]:
        action = sub.add_parser(name)
        action.add_argument("job")


def run(args):
    from runtime.persistence import JSONFileBackend
    from core.capabilities.registry import CapabilityRegistry
    from core.services.runtime import ServiceRuntime
    from core.services.integration import database_path
    from core.operations.store import OperationsStore

    storage = JSONFileBackend(args.store)
    registry = CapabilityRegistry(storage)
    runtime = ServiceRuntime(storage, registry, database_path(storage))
    command = args.service_command
    if command == "init-engineer":
        runtime.ensure_engineer()
        result = {
            "identity": "engineer",
            "persistent": True,
            "availability": "available",
            "execution": "bounded local artifacts",
        }
    elif command == "directory":
        result = [json.loads(r["document"]) for r in runtime.store.rows("SELECT document FROM services")]
    else:
        session = runtime.bind(args.identity)
        if command == "status":
            result = {
                "identity": args.identity,
                "spec": storage.load(args.identity, "identity_spec"),
                "jobs": runtime.store.jobs(args.identity),
                "balance": runtime.store.balance(args.identity),
                "reputation": runtime.store.reputation(args.identity),
            }
        elif command == "jobs":
            result = runtime.store.jobs(args.identity)
        elif command == "show":
            result = runtime.store.job(args.job)
            if args.identity not in (result["requester"], result["provider"]):
                raise PermissionError("not a job participant")
        elif command == "messages":
            result = runtime.receive(session)
        elif command == "relationships":
            runtime.sync_relationships(args.identity)
            result = [r.to_dict() for r in OperationsStore(storage, args.identity).list_relationships()]
        elif command == "reputation":
            result = runtime.store.reputation(args.identity)
        elif command == "balance":
            result = {"unit": "IDC", "redeemable": False, "balance": runtime.store.balance(args.identity)}
        elif command == "ledger":
            result = runtime.store.rows("SELECT * FROM entries WHERE account=?", (args.identity,))
        elif command == "provenance":
            result = runtime.store.rows("SELECT * FROM events WHERE identity=? ORDER BY seq", (args.identity,))
        elif command == "artifacts":
            result = runtime.store.rows("SELECT hash FROM artifacts")
        elif command == "recover":
            result = {"reconciled_interrupted": runtime.recover(session)}
        elif command == "specifications":
            result = runtime.negotiation.list(session)
        elif command == "propose":
            result = {"specification": runtime.negotiation.propose(session, args.provider, json.loads(args.contract.read_text()))}
        elif command == "respond":
            result = runtime.negotiation.respond(session, args.reference, args.revision, args.decision,
                       contract=json.loads(args.contract.read_text()) if args.contract else None)
        elif command == "start-job":
            result = {"job": runtime.negotiation.start_job(session, args.reference)}
        elif command == "request":
            result = {"job": runtime.request(session, args.provider, json.loads(args.contract.read_text()))}
        elif command == "quote":
            result = runtime.quote(session, args.job)
        elif command == "accept-quote":
            result = runtime.accept_quote(session, args.job)
        elif command == "accept-delivery":
            result = runtime.accept_delivery(session, args.job)
        elif command == "cancel":
            result = runtime.cancel(session, args.job)
        elif command == "rework":
            result = runtime.rework(session, args.job)
        elif command == "requester-tick":
            from core.services.worker import requester_tick

            result = {"state": requester_tick(runtime, args.identity)}
        elif command == "tick":
            from core.services.worker import tick

            result = {"state": tick(runtime, args.identity)}
        elif command == "serve":
            import time
            import fcntl
            from core.services.worker import tick

            # A process lock prevents two workers from recovering each other's work.
            lock_path = (
                Path(args.store)
                / ".identityos"
                / ("services-" + __import__("hashlib").sha256(args.identity.encode()).hexdigest() + ".lock")
            )
            with lock_path.open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                runtime.recover(session)
                while True:
                    tick(runtime, args.identity)
                    time.sleep(max(1.0, args.interval))
        else:
            raise ValueError("unsupported command")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0
