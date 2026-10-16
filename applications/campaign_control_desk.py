"""Campaign-owned capability contracts registered with the shared Core."""
from __future__ import annotations

from core.contracts import Capability


def capability_package(provider, validate_draft):
    context_schema = {"type": "object", "required": ["workflow", "dependencies"], "properties": {"workflow": {"type": "object"}, "dependencies": {"type": "object"}}}

    def generate(context):
        active_provider = provider() if callable(provider) else provider
        return {"draft": active_provider.generate(context["workflow"]["brief"])}

    def validate(context):
        brief = context["workflow"]["brief"]
        draft = context["dependencies"]["generate-copy"]["draft"]
        checks = validate_draft(brief, draft)
        return {"valid": all(item["ok"] for item in checks), "checks": checks}

    draft_schema = {"type": "object", "required": ["headline", "body", "highlights", "disclaimer"], "properties": {
        "headline": {"type": "string"}, "body": {"type": "string"},
        "highlights": {"type": "array", "items": {"type": "string"}}, "disclaimer": {"type": "string"},
    }}
    metadata_provider = provider() if callable(provider) else provider
    checks_schema = {"type": "array", "items": {"type": "object", "required": ["id", "ok"], "properties": {"id": {"type": "string"}, "ok": {"type": "boolean"}}}}
    capabilities = [
        Capability("campaign.copy.generate", "Generate a bounded real-estate campaign draft", ("campaign_copy_generation",), context_schema,
                   {"type": "object", "required": ["draft"], "properties": {"draft": draft_schema}}, generate, "marketing",
                   max_attempts=1, timeout_ms=25_000, provider=metadata_provider.name, metadata={"application": "campaign-control-desk"}),
        Capability("campaign.copy.validate", "Apply deterministic campaign copy safeguards", ("campaign_copy_validation",), context_schema,
                   {"type": "object", "required": ["valid", "checks"], "properties": {"valid": {"type": "boolean"}, "checks": checks_schema}}, validate, "marketing",
                   max_attempts=1, provider="deterministic-validation", metadata={"application": "campaign-control-desk"}),
    ]
    workflow = {
        "slug": "campaign-copy-draft",
        "version": 2,
        "name": "Campaign copy drafting and validation",
        "description": "Provider generation → deterministic real-estate copy validation. Application rules remain campaign-owned.",
        "input_schema": {"type": "object", "required": ["brief", "campaign_id"], "properties": {"campaign_id": {"type": "string"}, "brief": {"type": "object"}}},
        "steps": [
            {"key": "generate-copy", "capability": "campaign.copy.generate", "task_type": "campaign_copy_generation", "depends_on": []},
            {"key": "validate-copy", "capability": "campaign.copy.validate", "task_type": "campaign_copy_validation", "depends_on": ["generate-copy"]},
        ],
    }
    return capabilities, workflow
