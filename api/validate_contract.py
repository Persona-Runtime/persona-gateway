"""Offline documentation checks. Does not contact Kubernetes, DB, or the API."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate
from referencing import Registry, Resource


def main() -> None:
    root = Path(__file__).resolve().parent
    spec = json.loads((root / "openapi.json").read_text(encoding="utf-8"))
    validate(spec)
    urn = "urn:persona:openapi"
    resource = Resource.from_contents(
        {"$schema": "https://json-schema.org/draft/2020-12/schema", **spec}
    )
    registry = Registry().with_resource(urn, resource)

    def validator(name: str) -> Draft202012Validator:
        return Draft202012Validator(
            {"$ref": f"{urn}#/components/schemas/{name}"},
            registry=registry,
            format_checker=FormatChecker(),
        )

    operations = [
        (method, path, op)
        for path, item in spec["paths"].items()
        for method, op in item.items()
        if method in {"get", "post", "patch", "delete"}
    ]
    assert len(operations) == 22
    ids = [op["operationId"] for _, _, op in operations]
    assert len(ids) == len(set(ids)), "operationId duplicates"
    assert spec["security"] == [{"BearerAuth": []}]
    assert len(spec["paths"]) == 16
    for method, path, op in operations:
        assert "security" not in op, f"unexpected auth override: {path}"
        if method != "get":
            assert {"$ref": "#/components/parameters/IdempotencyKey"} in op[
                "parameters"
            ], f"missing idempotency contract: {method} {path}"

    examples = json.loads((root / "contract-examples.json").read_text(encoding="utf-8"))
    count = 0
    for case in examples["cases"]:
        valid = validator(case["schema"]).is_valid(case["value"])
        assert valid == case["valid"], f"unexpected fixture result: {case['name']}"
        count += 1

    question_boundaries = 0
    for character in ("a", "가", "😀"):
        for length, expected in ((2000, True), (2001, False)):
            request = {
                "conversation_id": "00000000-0000-4000-8000-000000000003",
                "message": character * length,
            }
            assert validator("ChatRequest").is_valid(request) == expected
            question_boundaries += 1

    policy = spec["x-service-policy"]
    expected_policy = {
        "max_personas_per_user": 3,
        "deleting_personas_count_toward_limit": True,
        "max_source_bytes_per_user": 104857600,
        "max_question_code_points": 2000,
        "max_output_tokens": 512,
        "max_active_generations_per_user": 1,
        "generation_queue_enabled": False,
        "first_answer_timeout_seconds": 60,
        "total_generation_timeout_seconds": 180,
        "timeout_origin": "generation_admitted_at",
        "release_slot_only_after_execution_termination": True,
        "browser_token_storage": "memory_only",
        "unused_version_retention_days": 7,
        "completed_deletion_record_retention_days": 7,
    }
    for key, expected in expected_policy.items():
        assert policy[key] == expected, f"policy drift: {key}"
    assert spec["info"]["version"] == "1.0.0-draft.2"

    sse_count = 0
    for _, _, op in operations:
        if "x-sse-events" not in op:
            continue
        media = op["responses"]["200"]["content"]
        assert "application/json" in media, "SSE replay JSON response is missing"
        wire = media["text/event-stream"]["example"]
        events = []
        for frame in wire.strip().split("\n\n"):
            lines = frame.splitlines()
            event = next(line[7:] for line in lines if line.startswith("event: "))
            data = json.loads(
                "\n".join(line[6:] for line in lines if line.startswith("data: "))
            )
            schema = op["x-sse-events"][event]["$ref"].rsplit("/", 1)[-1]
            validator(schema).validate(data)
            events.append(event)
            sse_count += 1
        assert events == ["meta", "citations", "delta", "done"]

    print("OpenAPI 3.1 validation passed: 22 operations / 16 paths")
    print(f"Synthetic schema fixtures passed: {count}; SSE example frames: {sse_count}")
    print(f"Question boundary cases passed: {question_boundaries}; policy values checked")
    print("No runtime, DB, Kubernetes, or business-state E2E validation performed.")


if __name__ == "__main__":
    main()
