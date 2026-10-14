"""Trusted stage shapes, shared by prompts, native grammar and request binding."""

MAX_INTENDED_TEST_FILES = 1  # One bounded edit completion per target.


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def structured_frontend_create(stage, target, evidence):
    return (target == 'frontend' and stage in {'edits', 'repair'}
            and type(evidence) is dict and evidence.get('test_operation') == 'create')


def stage_shape(stage, target=None, evidence=None):
    if structured_frontend_create(stage, target, evidence):
        return {
            'subject_imports': {'type': 'array', 'minItems': 1, 'maxItems': 64,
                                'uniqueItems': True,
                                'items': {'enum': evidence['available_subject_exports']}},
            'package_imports': {'type': 'array', 'maxItems': 64 if evidence['allowed_package_roots'] else 0,
                               'items': object_schema({
                'module': ({'enum': evidence['allowed_package_roots']} if evidence['allowed_package_roots']
                           else {'type': 'string', 'maxLength': 0}),
                'names': {'type': 'array', 'minItems': 1, 'maxItems': 64, 'uniqueItems': True,
                          'items': {'type': 'string', 'maxLength': 128,
                                    'pattern': '^[A-Za-z_$][A-Za-z0-9_$]*$'}},
            })},
            'test_body': {'type': 'string', 'minLength': 1, 'maxLength': 16384},
        }
    if stage == "decision":
        return {
            **({"selected_source_path": {"type": ["string", "null"]}}
               if target == "frontend" else {}),
            "action": {"enum": ["generate", "update", "no-tests-needed", "unsupported"]},
            "changed_behavior": {"type": "string"},
            "existing_test_evidence": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"},
            "intended_test_files": {"type": "array", "maxItems": MAX_INTENDED_TEST_FILES,
                                    "items": {"type": "string"}},
        }
    if stage == "edits":
        return {"edits": {"type": "array", "items": object_schema({
            "path": {"type": "string"}, "operation": {"enum": ["create", "update"]},
            "original_sha256": {"type": ["string", "null"]}, "content": {"type": "string"},
        })}}
    if stage == "repair":
        return {"replacement_content": {"type": "string", "minLength": 1}}
    raise ValueError("unsupported trusted stage")


def stage_schema(stage, target, binding, evidence=None, *, constrain_actions=False):
    shape = stage_shape(stage, target, evidence)
    if stage == "decision" and type(evidence) is dict:
        tests = evidence.get("existing_tests")
        candidates = evidence.get("candidate_tests")
        if type(tests) is list and type(candidates) is list:
            if target == "frontend":
                subjects = evidence.get("selectable_source_paths", [])
                shape["selected_source_path"] = {"enum": [None, *subjects]}
            available = sorted({e["path"] for e in tests if e.get("state") == "available"})
            paths = sorted({e["path"] for e in candidates})
            shape["existing_test_evidence"] = ({"type": "array", "items": {"enum": available}}
                                               if available else {"type": "array", "maxItems": 0,
                                                                  "items": {"type": "string"}})
            if paths:
                shape["intended_test_files"] = {"type": "array", "maxItems": MAX_INTENDED_TEST_FILES,
                                                "items": {"enum": paths}}
    schema = object_schema({"binding": {"const": binding}, "target": {"const": str(target)}, **shape})
    if constrain_actions and stage == "decision" and type(evidence) is dict:
        tests, candidates = evidence.get("existing_tests"), evidence.get("candidate_tests")
        if type(tests) is list and type(candidates) is list:
            available = {e["path"] for e in tests if e.get("state") == "available"}
            creates = sorted(e["path"] for e in candidates if e["exists"] is False)
            updates = sorted(e["path"] for e in candidates if e["exists"] is True and e["path"] in available)
            variants = []
            for action, paths in (("generate", creates), ("update", updates),
                                  ("no-tests-needed", None), ("unsupported", None)):
                if paths == [] or action == "no-tests-needed" and not available:
                    continue
                properties = {**schema["properties"], "action": {"const": action}}
                if target == "frontend":
                    subjects = evidence.get("selectable_source_paths", [])
                    if paths is not None and not subjects:
                        continue
                    properties["selected_source_path"] = ({"enum": subjects} if paths is not None
                                                          else {"const": None})
                properties["intended_test_files"] = ({"type": "array", "minItems": 1, "maxItems": 1,
                                                       "items": {"enum": paths}} if paths is not None else
                                                      {"type": "array", "maxItems": 0, "items": {"type": "string"}})
                if action == "no-tests-needed":
                    properties["existing_test_evidence"] = {**properties["existing_test_evidence"], "minItems": 1}
                variants.append(object_schema(properties))
            return {"oneOf": variants}
    return schema
