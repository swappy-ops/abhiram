"""Small independent documentation workflow used to prove Core reuse."""
from __future__ import annotations

import re

from core.contracts import Capability


INPUT_SCHEMA = {
    "type": "object",
    "required": ["title", "source_text", "source_label"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 160},
        "source_text": {"type": "string", "minLength": 1, "maxLength": 20000},
        "source_label": {"type": "string", "minLength": 1, "maxLength": 240},
    },
}


def retrieve_source(context: dict) -> dict:
    source = context["workflow"]["source_text"].strip()
    # Retrieved input remains explicitly untrusted; it is summarized, not executed.
    sentences = re.split(r"(?<=[.!?])\s+", source)
    summary = " ".join(sentences[:4]).strip()[:1800]
    return {"summary": summary, "source_label": context["workflow"]["source_label"], "provenance": "operator-supplied source text"}


def generate_document(context: dict) -> dict:
    research = context["dependencies"]["retrieve-source"]
    title = context["workflow"]["title"].strip()
    content = f"# {title}\n\n## Summary\n\n{research['summary']}\n\n## Source and verification\n\nSource: {research['source_label']}. This local artifact is based only on the supplied text; verify material claims against the original source before external use."
    return {"title": title, "content": content, "source_label": research["source_label"]}


def validate_document(context: dict) -> dict:
    artifact = context["dependencies"]["generate-document"]
    findings = []
    if not artifact.get("title", "").strip():
        findings.append("Document title is required")
    if len(artifact.get("content", "").strip()) < 80:
        findings.append("Document content is too short")
    if not artifact.get("source_label", "").strip():
        findings.append("Source provenance is required")
    return {"valid": not findings, "findings": findings, "artifact": artifact}


def capabilities() -> list[Capability]:
    context = {"type": "object", "required": ["workflow", "dependencies"], "properties": {"workflow": {"type": "object"}, "dependencies": {"type": "object"}}}
    return [
        Capability("docs.source.retrieve", "Retrieve and summarize operator-provided source material", ("source_retrieval",), context,
                   {"type": "object", "required": ["summary", "source_label", "provenance"], "properties": {"summary": {"type": "string"}, "source_label": {"type": "string"}, "provenance": {"type": "string"}}}, retrieve_source, "business-administration", provider="deterministic-local", metadata={"live": False}),
        Capability("docs.document.generate", "Prepare a source-attributed Markdown document", ("document_generation",), context,
                   {"type": "object", "required": ["title", "content", "source_label"], "properties": {"title": {"type": "string"}, "content": {"type": "string"}, "source_label": {"type": "string"}}}, generate_document, "business-administration", provider="deterministic-local", metadata={"live": False}),
        Capability("docs.document.validate", "Validate document completeness and provenance", ("document_validation",), context,
                   {"type": "object", "required": ["valid", "findings", "artifact"], "properties": {"valid": {"type": "boolean"}, "findings": {"type": "array", "items": {"type": "string"}}, "artifact": {"type": "object"}}}, validate_document, "business-administration", provider="deterministic-local", metadata={"live": False}),
    ]


def workflow_definition() -> dict:
    return {
        "slug": "documentation-pipeline",
        "version": 1,
        "name": "Documentation pipeline",
        "description": "Source retrieval → document generation → provenance validation.",
        "input_schema": INPUT_SCHEMA,
        "steps": [
            {"key": "retrieve-source", "capability": "docs.source.retrieve", "task_type": "source_retrieval", "depends_on": []},
            {"key": "generate-document", "capability": "docs.document.generate", "task_type": "document_generation", "depends_on": ["retrieve-source"]},
            {"key": "validate-document", "capability": "docs.document.validate", "task_type": "document_validation", "depends_on": ["generate-document"]},
        ],
    }
