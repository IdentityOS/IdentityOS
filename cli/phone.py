"""Phone administration. No public listener or paid provider by default."""

import asyncio
import getpass
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit


def local_model_url(config):
    url = config.get("model_base_url", "http://127.0.0.1:11434/v1")
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Phone model_base_url must be a local HTTP endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Phone model_base_url must not contain credentials, query or fragment")
    return url


def phone_adapter(config):
    from adapters.openai_adapter import OllamaAdapter, OpenAIAdapter

    if config.get("provider_env_file"):
        from dotenv import dotenv_values

        from adapters.chain import ChainAdapter
        from adapters.configuration import build_adapter_from_env

        # Deliberately select providers instead of importing an entire .env into
        # this process (it may also contain GitHub and unrelated credentials).
        source = dotenv_values(config["provider_env_file"], interpolate=False)
        providers = config.get("cloud_providers", ["groq", "cerebras", "openrouter"])
        if not providers or any(p not in ("groq", "cerebras", "openrouter") for p in providers):
            raise ValueError("Phone cloud_providers must select groq, cerebras or openrouter")
        adapters = []
        for provider in dict.fromkeys(providers):
            prefix = provider.upper() + "_"
            values = {k: v for k, v in source.items() if k.startswith(prefix) and v}
            values["OPENAI_TIMEOUT"] = str(config.get("model_timeout", 20))
            adapter = build_adapter_from_env(values)
            if adapter is None:
                continue
            adapter.timeout = float(config.get("model_timeout", 20))
            adapter.max_tokens = int(config.get("max_tokens", 256))
            adapters.append(adapter)
        if not adapters:
            raise ValueError("No configured phone cloud provider credentials found")
        return ChainAdapter(adapters, cooldown_seconds=60)

    mode = config.get("tool_mode", "native")
    if mode not in ("native", "legacy"):
        raise ValueError("Phone tool_mode must be native or legacy")
    options = dict(
        model=config["model"],
        base_url=local_model_url(config),
        max_tokens=256,
        timeout=float(config.get("model_timeout", 120)),
    )
    if mode == "legacy":
        return OllamaAdapter(**options, prefer_legacy_tools=True)
    return OpenAIAdapter(**options, api_key="ollama")


def add_parser(sub):
    parser = sub.add_parser("phone", help="Local IdentityOS switchboard")
    parser.add_argument("--config", default=".identity_phone/config.json")
    commands = parser.add_subparsers(dest="phone_command", required=True)
    for name in ("status", "callers", "routes"):
        commands.add_parser(name)
    serve = commands.add_parser("serve")
    serve.add_argument("--agi-port", type=int, default=4573)
    serve.add_argument("--audio-port", type=int, default=9092)
    default = commands.add_parser("default")
    default.add_argument("--user", required=True)
    default.add_argument("--identity", required=True)
    bind = commands.add_parser("bind")
    bind.add_argument("--caller", required=True)
    bind.add_argument("--user", required=True)
    bind.add_argument("--identity")
    pin = commands.add_parser("pin").add_subparsers(dest="pin_command", required=True).add_parser("set")
    pin.add_argument("--user", required=True)
    provider = commands.add_parser("provider").add_subparsers(dest="provider_command", required=True)
    provider.add_parser("list")
    configure = provider.add_parser("configure")
    configure.add_argument("--reference", required=True, help="Path to local configuration, never inline secrets")
    sms = commands.add_parser("sms", help="Local SMS ingress test; does not send carrier SMS")
    sms.add_argument("--caller", required=True)
    sms.add_argument("text")


def run(args):
    from cli.main import _get_storage
    from core.channels.router import IdentityRouter
    from runtime.orchestrator import IdentityRuntime
    from runtime.phone.switchboard import Switchboard

    config_path = Path(args.config).resolve()
    try:
        config = json.loads(config_path.read_text())
        router = IdentityRouter(config_path.parent / "routing.sqlite3", config["identities"])
        command = args.phone_command
        if command in ("status", "routes"):
            print(
                json.dumps(
                    {
                        "provider": "asterisk",
                        "listeners": "loopback only",
                        "identities": router.identities,
                        "state": str(router.path),
                        "note": "Configuration status only; does not prove a live call.",
                    },
                    indent=2,
                )
            )
        elif command == "callers":
            print(json.dumps(router.callers(), indent=2))
        elif command == "bind":
            router.bind(args.caller, args.user, args.identity)
        elif command == "default":
            router.default(args.user, args.identity)
        elif command == "pin":
            pin = getpass.getpass("New PIN (6–12 digits): ")
            if pin != getpass.getpass("Repeat PIN: "):
                raise ValueError("PINs do not match")
            router.set_pin(args.user, pin)
        elif command == "provider":
            if args.provider_command == "configure":
                ref = str(Path(args.reference).resolve(strict=True))
                with router.db() as db:
                    db.execute("INSERT OR REPLACE INTO providers VALUES(?,?)", ("asterisk", ref))
            else:
                print("asterisk: local SIP via FastAGI + AudioSocket. PSTN providers: not installed.")
        else:
            from runtime.phone.asterisk import PhoneGateway
            from runtime.phone.audio import EnergyVAD, FasterWhisper, Piper, WhisperCpp

            adapter = phone_adapter(config)
            runtime = IdentityRuntime(storage=_get_storage(args), adapter=adapter)
            try:
                runtime.load_persisted()
                for identity_id in router.identities.values():
                    if runtime.identity_store.get(identity_id) is None:
                        raise ValueError(f"Identity is not loaded: {identity_id}")
                board = Switchboard(router, runtime)
                if command == "sms":
                    print(board.sms(args.caller, args.text))
                else:
                    logging.basicConfig(level=logging.INFO)
                    stt_config = config["stt"]
                    if stt_config["backend"] == "faster-whisper":
                        stt = FasterWhisper(stt_config["model"], vocabulary=router.identities)
                    elif stt_config["backend"] == "whisper.cpp":
                        stt = WhisperCpp(stt_config["executable"], stt_config["model"])
                    else:
                        raise ValueError("Unknown STT backend")
                    tts = Piper(config["tts"]["executable"], config["tts"]["model"])
                    gateway = PhoneGateway(
                        board, stt, tts, EnergyVAD(config.get("vad_threshold", 500)), audio_port=args.audio_port
                    )
                    print(
                        f"Starting IdentityOS phone: FastAGI 127.0.0.1:{args.agi_port}; "
                        f"AudioSocket 127.0.0.1:{args.audio_port}",
                        flush=True,
                    )
                    asyncio.run(gateway.serve(args.agi_port))
            finally:
                runtime.shutdown()
    except (OSError, ValueError, KeyError, PermissionError) as exc:
        print(f"Phone configuration/action failed: {exc}")
        return 1
    except KeyboardInterrupt:
        return 0
    return 0
