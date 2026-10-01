"""Provider contract metadata; request support is not runtime evidence of truth."""


def expression_schema(snapshot):
    from core.expression import catalog

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["snapshot_id", "facts_used", "inferences", "proposals"],
        "properties": {
            "snapshot_id": {"type": "string", "enum": [snapshot["snapshot_id"]]},
            "facts_used": {
                "type": "array",
                "minItems": 1,
                "maxItems": 60,
                "items": {"type": "string", "enum": list(catalog(snapshot))},
            },
            "inferences": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 400}},
            "proposals": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 400}},
        },
    }


def options(adapter, snapshot):
    # Legacy/custom adapters remain prompt-only; do not assume arbitrary kwargs support.
    from adapters.base import BaseAdapter

    if not isinstance(adapter, BaseAdapter):
        return {}
    return {"_response_schema": expression_schema(snapshot), "_generation_budget": 20.0}


def compatible_kwargs(adapter, kwargs):
    """Inspect before calling: a TypeError after a tool effect must not replay it."""
    import inspect

    parameters = inspect.signature(adapter.generate).parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in parameters}
