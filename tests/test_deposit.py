"""The ad form rules and the create/submit guards, on a synthetic form only."""

import json

import pytest

from leboncoin_mcp import adform
from leboncoin_mcp.deposit import (
    approved_create,
    create_refusal,
    photo_files,
    submit_refusal,
)
from leboncoin_mcp.errors import ActionRefused, UnexpectedResponse

V = "DECORATION_VISUAL_REPRESENTATION_"


def q(key, widget, answer, label=None, mandatory=False, regex=None, when=None, max_length=None):
    item = {
        "identifier": f"{key}_id",
        "type": "ITEM_TYPE_QUESTION",
        "answer_modelization": {"representation": {"associated_key": key}, **answer},
        "decoration": {"visual_representation": V + widget, "label": label or key},
    }
    if max_length:
        item["decoration"]["max_length"] = max_length
    rules = {}
    if mandatory:
        rules["mandatory"] = {"error_message": "required"}
    if regex:
        rules["regex"] = [{"value": regex, "error_message": "bad format"}]
    if rules:
        item["static_rules"] = rules
    if when:
        item["dynamic_rules"] = {"conditional_existence": when}
    return item


def choices(*pairs, default=None):
    answer = {"choices": [{"identifier": i, "label": label} for i, label in pairs]}
    if default:
        answer["default_choices"] = default
    return {"multiple_answers": answer}


def single(kind, default=None):
    answer = {"type": f"QUESTION_ANSWER_TYPE_{kind}"}
    if default is not None:
        answer["default_answer"] = default
    return {"single_answer": answer}


def equal(key, *values, operator="EQUAL"):
    return [{"existence_requirements": [
        {"previous_associated_key": key, "previous_raw_answer": list(values),
         "operator": f"REQUIREMENT_OPERATOR_{operator}"}
    ]}]


ITEMS = [
    q("location", "LOCATION", single("LOCATION"), mandatory=True),
    q("has_street_view", "TOGGLE", choices(("2", "Non"), ("1", "Oui"), default=["1"]),
      when=[{"existence_requirements": [{"previous_associated_key": "location",
                                         "operator": "REQUIREMENT_OPERATOR_IS_ANSWERED"}]}]),
    q("real_estate_type", "SELECT_CELLS_WITH_ICON", choices(("1", "Maison"), ("2", "Appartement")),
      mandatory=True),
    q("furnished", "RADIO", choices(("2", "Non meublé"), ("1", "Meublé"), default=["2"]),
      when=equal("real_estate_type", "1", "2")),
    q("floor_number", "TEXTFIELD_INT", single("INT"), when=equal("real_estate_type", "2")),
    q("square", "TEXTFIELD_FLOAT", single("FLOAT"), label="Surface", mandatory=True,
      regex=r"^\d{1,6}(?:[\.]\d{1,2})?$"),
    q("outside_access", "MULTI_SELECT_CELLS", choices(("balcony", "Balcon"), ("garden", "Jardin"))),
    q("pet_accepted", "TOGGLE", choices(("1", "Non"), ("2", "Oui"), default=["2"])),
    q("images", "IMAGES", single("IMAGES")),
    q("subject", "TEXTFIELD_TEXT", single("STRING"), mandatory=True, max_length=20),
    q("body", "TEXTAREA", single("STRING"), mandatory=True, regex=r"[^\s](.|\n){8,}[^\s]"),
    q("price", "PRICE", single("PRICE"), mandatory=True, regex=r"^[1-9]\d*$"),
    q("phone_hidden", "TOGGLE", single("BOOL", default="true")),
    q("ad_submission_id", "HIDDEN", single("STRING")),
]
CONFIG = {
    "navigation": {"ordered_steps": [
        {"identifier": "STEP_IDENTIFIER_LOCATION", "title": "Où ?", "ordered_items": ["location_id", "has_street_view_id"]},
        {"identifier": "STEP_IDENTIFIER_ADPARAMS", "title": "Détails", "ordered_items": [
            "real_estate_type_id", "furnished_id", "floor_number_id", "square_id",
            "outside_access_id", "pet_accepted_id"]},
        {"identifier": "STEP_IDENTIFIER_IMAGES", "title": "Photos", "ordered_items": ["images_id"]},
        {"identifier": "STEP_IDENTIFIER_DESCRIPTION", "title": "Texte",
         "ordered_items": ["subject_id", "body_id"]},
        {"identifier": "STEP_IDENTIFIER_PRICE", "title": "Prix", "ordered_items": ["price_id"]},
        {"identifier": "STEP_IDENTIFIER_CONTACT", "title": "Contact",
         "ordered_items": ["phone_hidden_id", "ad_submission_id_id"]},
    ]},
    "definitions": {"items": ITEMS},
}
FIXED = {"location": ("address",), "images": ("x",)}
GOOD = {"subject": "Chambre", "body": "Une belle chambre.", "price": 325, "phone_hidden": True,
        "real_estate_type": "Maison", "square": 90}


@pytest.fixture
def fields():
    return adform.parse_form(CONFIG)


def test_parse_keeps_questions_in_order_without_hidden_ones(fields):
    keys = [f.key for f in fields]
    assert keys[0] == "location" and "ad_submission_id" not in keys
    assert keys.index("real_estate_type") < keys.index("furnished") < keys.index("subject")
    square = next(f for f in fields if f.key == "square")
    assert (square.mandatory, square.step_title, square.widget) == (True, "Détails", "TEXTFIELD_FLOAT")


def test_an_overriding_item_replaces_the_old_one():
    config = json.loads(json.dumps(CONFIG))
    newer = q("outside_access", "MULTI_SELECT_CELLS", choices(("terrace", "Terrasse")))
    newer["identifier"], newer["overrides"] = "new_outside_access_id", "outside_access_id"
    config["definitions"]["items"].append(newer)
    field = next(f for f in adform.parse_form(config) if f.key == "outside_access")
    assert field.widget == "MULTI_SELECT_CELLS"
    assert field.choices_for({}) == {"terrace": "Terrasse"}


def test_valid_ad_resolves_labels_to_ids(fields):
    resolved = adform.resolve(fields, GOOD, FIXED)
    values = {f.key: v for f, v in resolved.fields}
    assert values["real_estate_type"] == ("1",)
    assert values["square"] == ("90",)
    assert values["phone_hidden"] == ("true",)
    assert values["price"] == ("325",)
    # applies but not given, and has no default: offered to the client
    assert [f.key for f in resolved.optional] == ["outside_access"]


def test_every_problem_is_reported_at_once(fields):
    bad = {**GOOD, "real_estate_type": "Château", "square": "90m2", "subject": "x" * 21,
           "price": 0, "colour": "red"}
    del bad["body"]
    with pytest.raises(ActionRefused) as exc:
        adform.resolve(fields, bad, FIXED)
    message = str(exc.value)
    for part in ("Château is not allowed", "bad format", "at most 20 characters", "body", "colour"):
        assert part in message


def test_fields_follow_earlier_answers(fields):
    # floor_number only applies to flats
    with pytest.raises(ActionRefused, match="does not apply"):
        adform.resolve(fields, {**GOOD, "floor_number": 2}, FIXED)
    resolved = adform.resolve(fields, {**GOOD, "real_estate_type": "2", "floor_number": 2}, FIXED)
    assert ("floor_number", ("2",)) in [(f.key, v) for f, v in resolved.fields]


def test_multiple_choices_and_toggles(fields):
    resolved = adform.resolve(
        fields, {**GOOD, "outside_access": ["Balcon", "garden"], "pet_accepted": False}, FIXED
    )
    values = {f.key: v for f, v in resolved.fields}
    assert values["outside_access"] == ("balcony", "garden")
    assert values["pet_accepted"] == ("1",)  # « Non »


def test_street_view_applies_once_an_address_is_chosen(fields):
    resolved = adform.resolve(fields, {**GOOD, "has_street_view": False}, FIXED)
    assert ("has_street_view", ("2",)) in [(f.key, v) for f, v in resolved.fields]
    with pytest.raises(ActionRefused, match="does not apply"):
        adform.resolve(fields, {**GOOD, "has_street_view": False}, {"images": ("x",)})


def test_single_choice_refuses_two_values(fields):
    with pytest.raises(ActionRefused, match="single value"):
        adform.resolve(fields, {**GOOD, "real_estate_type": ["1", "2"]}, FIXED)


def test_unknown_rule_is_refused():
    config = json.loads(json.dumps(CONFIG))
    config["definitions"]["items"][3]["dynamic_rules"]["conditional_existence"][0][
        "existence_requirements"][0]["operator"] = "REQUIREMENT_OPERATOR_GREATER"
    with pytest.raises(UnexpectedResponse):
        adform.parse_form(config)


# --- photos ---------------------------------------------------------------------------------


def test_photos_must_be_images_inside_the_folder(tmp_path):
    (tmp_path / "salon.jpg").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x")
    assert [p.name for p in photo_files(str(tmp_path), ["salon.jpg"])] == ["salon.jpg"]
    with pytest.raises(ActionRefused) as exc:
        photo_files(str(tmp_path), ["../secret.jpg", "notes.txt", "missing.jpg", "/etc/x.jpg"])
    assert str(exc.value).count("outside the photos folder") == 2
    assert "not a JPEG" in str(exc.value) and "no such file" in str(exc.value)
    with pytest.raises(ActionRefused):
        photo_files(str(tmp_path), [])
    with pytest.raises(ActionRefused, match="twice"):
        photo_files(str(tmp_path), ["salon.jpg", "./salon.jpg"])


# --- the guards -----------------------------------------------------------------------------


def approved():
    values = {"subject": ("Chambre",), "body": ("Une belle chambre.",), "price": ("325",),
              "phone_hidden": ("true",), "real_estate_type": ("1",), "outside_access": ("balcony",)}
    return approved_create("11", values, "Place du Martray, Pontivy (56300)", 2)


def create_body(**changes):
    payload = {
        "category_id": "11", "subject": "Chambre", "body": "Une belle chambre.", "price": "325",
        "images": [{"name": "a.jpg"}, {"name": "b.jpg"}], "phone_hidden": True,
        "location": {"label": "Place du Martray, Pontivy (56300)", "city": "Pontivy"},
        "attributes": {"real_estate_type": "1", "ad_submission_id": "generated"},
        "extended_attributes": {"outside_access": ["balcony"]},
        "email": "someone@example.org",
    }
    payload.update(changes)
    return json.dumps(payload).encode()


def test_exact_create_is_allowed():
    assert create_refusal(create_body(), approved()) is None


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"category_id": "10"}, "category_id"),
        ({"subject": "Chambre !"}, "subject"),
        ({"body": "Autre texte"}, "body"),
        ({"price": "1325"}, "price"),
        ({"images": [{"name": "a.jpg"}]}, "photos"),
        ({"location": {"label": "Ailleurs (75001)"}}, "address"),
        ({"phone_hidden": False}, "phone"),
        ({"attributes": {"real_estate_type": "2"}}, "real_estate_type"),
        ({"extended_attributes": {"outside_access": ["balcony", "pool"]}}, "outside_access"),
    ],
)
def test_any_difference_blocks_the_create(changes, reason):
    assert reason in create_refusal(create_body(**changes), approved())


def test_garbage_create_is_blocked():
    assert create_refusal(b"not json", approved())
    assert create_refusal(b"[]", approved())


def submit(options=(), ad_id=42, count=1):
    ad = {"ad_type": "let", "ad_id": ad_id, "options": list(options), "transaction_type": "new_ad"}
    return json.dumps({"ads": [ad] * count, "pricing_id": "x"}).encode()


def test_submit_without_options_is_allowed():
    assert submit_refusal(submit(), 42) is None


def test_paid_options_and_other_ads_are_blocked():
    assert "paid options" in submit_refusal(submit(options=["urgent"]), 42)
    assert "another ad" in submit_refusal(submit(ad_id=7), 42)
    assert "another ad" in submit_refusal(submit(), None)  # before the ad exists
    assert "exactly one" in submit_refusal(submit(count=2), 42)
