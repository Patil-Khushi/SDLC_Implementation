"""Tag -> requirement traceability: precise matching, and honesty about what does not match.

``_build_tag_reqs`` is the only thing connecting an OpenAPI tag to the requirement ids that end up
on a work item, so its mistakes are invisible: a work item simply carries the wrong
``requirement_ids``, or an empty list, and nothing downstream can tell the difference between
"no requirements" and "we could not work it out".

The original implementation matched a singularised tag stem as a **substring of the feature name
with every non-alphanumeric stripped out** ("Orders & Fulfilment" -> "ordersfulfilment"), and gave
the tag the UNION of every feature it loosely hit. That union is the damage these tests pin: a tag
silently inheriting two unrelated features' requirements, with nothing downstream able to tell.

Matching is now token-exact first, then a loose match accepted only when it is unambiguous, with
anything unresolved reported. Abbreviations still work - "auth" for "Account & Authentication" is
real and the live pack depends on it - but "two features, pick one silently" no longer does.
"""

from __future__ import annotations

import logging

from app.services.design_pack import DetectedFile, _build_tag_reqs


def _roles(features: list[dict]) -> dict[str, list[DetectedFile]]:
    return {
        "user_features": [
            DetectedFile(path=None, role="user_features", obj={"features": features})  # type: ignore[arg-type]
        ]
    }


def _features(*pairs: tuple[str, list[str]]) -> list[dict]:
    return [{"name": name, "requirements": reqs} for name, reqs in pairs]


# --------------------------------------------------------------- precision

def test_a_tag_matches_the_feature_that_names_it_as_a_word():
    roles = _roles(_features(("Orders & Fulfilment", ["REQ-1", "REQ-2"]),
                             ("Payments", ["REQ-9"])))
    assert _build_tag_reqs(roles, ["orders"]) == {"orders": ["REQ-1", "REQ-2"]}
    assert _build_tag_reqs(roles, ["payments"]) == {"payments": ["REQ-9"]}


def test_an_unambiguous_abbreviation_still_resolves():
    # Abbreviated tags are normal and the live pack depends on one: "auth" is not a word of
    # "Account & Authentication" but plainly means it. A single loose match is accepted.
    roles = _roles(_features(("Account & Authentication", ["REQ-35", "REQ-36"]),
                             ("Payments", ["REQ-9"])))
    assert _build_tag_reqs(roles, ["auth"]) == {"auth": ["REQ-35", "REQ-36"]}


def test_an_AMBIGUOUS_loose_match_is_refused_rather_than_unioned(caplog):
    # The real damage the old substring test did: "order" is inside both glued names, so the tag
    # silently inherited four requirements from two unrelated features. Refusing is safer than
    # guessing, because a wrong mapping is invisible downstream while a missing one is reported.
    roles = _roles(_features(("Order Fulfilment", ["REQ-1", "REQ-2"]),
                             ("Reorder Suggestions", ["REQ-80"])))
    with caplog.at_level(logging.WARNING):
        out = _build_tag_reqs(roles, ["orderz"])   # stem "orderz" is in neither token set
    assert "orderz" not in out
    # and when it IS ambiguous, it is named in the warning. (This used to be "Ordering" vs
    # "Reordering"; since stemming, "order" is precisely a word form of "Ordering" and NOT of
    # "Reordering", so that pair now resolves — see test_word_forms_match_by_stem. Here BOTH
    # feature names carry a form of "order", which stays refused.)
    with caplog.at_level(logging.WARNING):
        out2 = _build_tag_reqs(_roles(_features(("Ordering Flow", ["REQ-1"]),
                                                ("Ordered Items", ["REQ-80"]))), ["order"])
    assert "order" not in out2
    assert "matches" in caplog.text


def test_word_forms_match_by_stem():
    # The Fleet pack's tag "schedules" vs its feature "Service Scheduling": the same word, a
    # different form. Before stemming the tag mapped to nothing and its module was generated
    # without the scheduling requirements.
    roles = _roles(_features(("Service Scheduling", ["REQ-1", "REQ-2"]), ("Work Orders", ["REQ-4"])))
    assert _build_tag_reqs(roles, ["schedules"]) == {"schedules": ["REQ-1", "REQ-2"]}
    assert _build_tag_reqs(_roles(_features(("Ordering", ["REQ-1"]), ("Reordering", ["REQ-80"]))),
                           ["order"]) == {"order": ["REQ-1"]}


def test_a_tag_no_feature_names_takes_requirements_matching_its_operations():
    roles = _roles(_features(("Service Scheduling", ["REQ-1"])))
    roles["requirements"] = [DetectedFile(path=None, role="requirements", obj={"functional": [  # type: ignore[arg-type]
        {"id": "REQ-1", "text": "The system shall provide a depot dashboard listing vehicles due for service."},
        {"id": "REQ-2", "text": "The system shall let a manager enter an odometer reading manually."},
        {"id": "REQ-3", "text": "The system shall store vehicles."},
    ]})]
    endpoints = [
        {"method": "GET", "path": "/depots/{id}/dashboard", "tag": "depots", "operation_id": "getDepotDashboard",
         "summary": "Get depot service dashboard"},
        {"method": "POST", "path": "/readings", "tag": "readings", "operation_id": "createReading",
         "summary": "Submit an odometer reading (manual or telematics)"},
        {"method": "GET", "path": "/auth/me", "tag": "auth", "operation_id": "getCurrentUser",
         "summary": "Return authenticated user profile"},
    ]
    out = _build_tag_reqs(roles, ["depots", "readings", "auth"], endpoints)
    assert out == {"depots": ["REQ-1"], "readings": ["REQ-2"]}       # auth matches nothing: stays unmapped


def test_a_tag_does_not_collect_the_union_of_two_incidental_matches():
    # Old behaviour: stem "order" is a substring of BOTH glued names, so the tag took all four
    # requirements. Token matching gives it only the feature that actually names it.
    roles = _roles(_features(("Orders & Fulfilment", ["REQ-1", "REQ-2"]),
                             ("Reorder Suggestions", ["REQ-80", "REQ-81"])))
    assert _build_tag_reqs(roles, ["orders"]) == {"orders": ["REQ-1", "REQ-2"]}


def test_singularisation_works_in_both_directions():
    # tag plural vs feature singular, and the reverse.
    assert _build_tag_reqs(_roles(_features(("Payment", ["REQ-1"]))), ["payments"]) == {
        "payments": ["REQ-1"]
    }
    assert _build_tag_reqs(_roles(_features(("Payments", ["REQ-1"]))), ["payment"]) == {
        "payment": ["REQ-1"]
    }


def test_two_tags_of_one_feature_both_map_to_it():
    # A known and accepted limit: a tag inherits its whole feature's requirements, so `cart` and
    # `checkout` both get all of "Cart & Checkout". Pinned so the behaviour is deliberate.
    roles = _roles(_features(("Cart & Checkout", ["REQ-15", "REQ-16"])))
    out = _build_tag_reqs(roles, ["cart", "checkout"])
    assert out == {"cart": ["REQ-15", "REQ-16"], "checkout": ["REQ-15", "REQ-16"]}


def test_the_loose_fallback_rescues_a_glued_lowercase_feature_name():
    # "orderfulfilment" has no word boundary and no camelCase hump, so _tokens gives one glued
    # token and only the loose test can match it. Single match => accepted.
    roles = _roles(_features(("orderfulfilment", ["REQ-1"])))
    assert _build_tag_reqs(roles, ["fulfilment"]) == {"fulfilment": ["REQ-1"]}


# --------------------------------------------------------------- honesty

def test_an_unmatched_tag_is_absent_and_warned_about(caplog):
    roles = _roles(_features(("Orders & Fulfilment", ["REQ-1"])))
    with caplog.at_level(logging.WARNING):
        out = _build_tag_reqs(roles, ["orders", "returns", "serviceability"])

    assert out == {"orders": ["REQ-1"]}, "unmatched tags get no entry, not an empty list"
    assert "returns" in caplog.text and "serviceability" in caplog.text
    assert "requirement_ids=[]" in caplog.text, "the message must say what the consequence is"


def test_no_warning_when_everything_maps(caplog):
    roles = _roles(_features(("Payments", ["REQ-9"])))
    with caplog.at_level(logging.WARNING):
        _build_tag_reqs(roles, ["payments"])
    assert "map to no requirement ids" not in caplog.text


def test_the_warning_is_ascii_so_a_cp1252_console_cannot_break_the_run(caplog):
    # This module's logs reach a Windows console through the CLI scripts; a non-ASCII character in
    # the format string raises UnicodeEncodeError there and takes the run down with it.
    roles = _roles(_features(("Payments", ["REQ-9"])))
    with caplog.at_level(logging.WARNING):
        _build_tag_reqs(roles, ["returns"])
    for record in caplog.records:
        record.getMessage().encode("cp1252")   # must not raise


# --------------------------------------------------------------- degradation

def test_no_features_yields_no_mappings_rather_than_raising():
    assert _build_tag_reqs({}, ["orders", "payments"]) == {}


def test_features_without_requirements_are_ignored():
    roles = _roles(_features(("Orders", []), ("Payments", ["REQ-9"])))
    assert _build_tag_reqs(roles, ["orders", "payments"]) == {"payments": ["REQ-9"]}


def test_blank_tags_are_skipped():
    roles = _roles(_features(("Payments", ["REQ-9"])))
    assert _build_tag_reqs(roles, ["", "  ", "payments"]) == {"payments": ["REQ-9"]}


def test_the_requirements_fallback_groups_by_the_feature_field():
    # No user_features artifact: group the flat requirements list by its own `feature` field.
    roles = {
        "requirements": [
            DetectedFile(  # type: ignore[arg-type]
                path=None, role="requirements",
                obj={"functional": [
                    {"id": "REQ-1", "feature": "Orders & Fulfilment"},
                    {"id": "REQ-2", "feature": "Orders & Fulfilment"},
                    {"id": "REQ-9", "feature": "Payments"},
                ]},
            )
        ]
    }
    assert _build_tag_reqs(roles, ["orders", "payments"]) == {
        "orders": ["REQ-1", "REQ-2"],
        "payments": ["REQ-9"],
    }


def test_ids_are_deduplicated_and_sorted():
    roles = _roles(_features(("Orders", ["REQ-2", "REQ-1", "REQ-2"])))
    assert _build_tag_reqs(roles, ["orders"]) == {"orders": ["REQ-1", "REQ-2"]}
