"""The ad form, as Leboncoin describes it: steps, fields, allowed values and rules.

The deposit page builds its form from `dynamic-deposit/config`. Reading that config lets us check
an ad's values before typing anything, with Leboncoin's own rules and messages, and drive the
form field by field. No browser here: these are pure functions over the config.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import ActionRefused, UnexpectedResponse

WIDGET_PREFIX = "DECORATION_VISUAL_REPRESENTATION_"
# Fields handled outside the generic filler: the address and photos have their own steps, the
# contact details come from the account, and hidden fields are set by the page itself.
SPECIAL_KEYS = frozenset({"location", "images", "email", "phone"})
MULTIPLE_WIDGETS = frozenset({"MULTI_SELECT", "MULTI_SELECT_CELLS"})
OPERATORS = frozenset({"EQUAL", "NOT_EQUAL", "IS_ANSWERED", "IS_NOT_ANSWERED"})

Answers = Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class Requirement:
    key: str
    operator: str
    values: tuple[str, ...]

    def met(self, answers: Answers) -> bool:
        given = answers.get(self.key, ())
        if self.operator == "EQUAL":
            return any(v in self.values for v in given)
        if self.operator == "NOT_EQUAL":
            return not any(v in self.values for v in given)
        if self.operator == "IS_ANSWERED":
            return bool(given)
        return not given  # IS_NOT_ANSWERED


Groups = tuple[tuple[Requirement, ...], ...]


def any_group_met(groups: Groups, answers: Answers) -> bool:
    """No groups means always. Otherwise one group must have all its requirements met."""
    return not groups or any(all(r.met(answers) for r in group) for group in groups)


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    step: str
    step_title: str
    widget: str
    answer_type: str  # e.g. INT, PRICE, STRING, BOOL; "CHOICE" for choice lists
    mandatory: bool
    patterns: tuple[tuple[str, str], ...]  # (regex, Leboncoin's error message)
    max_length: int | None
    exists_when: Groups
    choices: tuple[tuple[Groups, tuple[tuple[str, str], ...]], ...]  # (when, ((id, label), ...))
    default: tuple[str, ...]

    @property
    def multiple(self) -> bool:
        return self.widget in MULTIPLE_WIDGETS

    def exists(self, answers: Answers) -> bool:
        return any_group_met(self.exists_when, answers)

    def choices_for(self, answers: Answers) -> dict[str, str] | None:
        """Allowed {id: label} given earlier answers, or None for a free value."""
        for when, choices in self.choices:
            if any_group_met(when, answers):
                return dict(choices)
        return None if not self.choices else {}


def _requirements(groups: Sequence[dict[str, Any]], name: str) -> Groups:
    parsed = []
    for group in groups or []:
        requirements = []
        for raw in group.get(name) or []:
            operator = str(raw.get("operator") or "").removeprefix("REQUIREMENT_OPERATOR_")
            if operator not in OPERATORS:
                raise UnexpectedResponse(f"The ad form uses an unknown rule ({operator}).")
            requirements.append(
                Requirement(
                    key=raw.get("previous_associated_key") or "",
                    operator=operator,
                    values=tuple(str(v) for v in raw.get("previous_raw_answer") or ()),
                )
            )
        parsed.append(tuple(requirements))
    return tuple(parsed)


def _choice_list(raw: Sequence[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    return tuple((str(c["identifier"]), str(c.get("label") or c["identifier"])) for c in raw or ())


def _field(item: dict[str, Any], step: str, step_title: str) -> Field | None:
    if item.get("type") != "ITEM_TYPE_QUESTION":
        return None
    decoration = item.get("decoration") or {}
    widget = str(decoration.get("visual_representation") or "").removeprefix(WIDGET_PREFIX)
    if widget == "HIDDEN":
        return None
    model = item.get("answer_modelization") or {}
    key = (model.get("representation") or {}).get("associated_key")
    if not key:
        return None
    rules = item.get("dynamic_rules") or {}
    static = item.get("static_rules") or {}
    if "multiple_answers" in model:
        multiple = model["multiple_answers"] or {}
        answer_type = "CHOICE"
        choices: list[tuple[Groups, tuple[tuple[str, str], ...]]] = []
        if multiple.get("choices"):
            choices.append(((), _choice_list(multiple["choices"])))
        for conditional in rules.get("conditional_choices") or []:
            when = _requirements([conditional], "choices_requirements")
            choices.append((when, _choice_list(conditional.get("choices") or ())))
        default = tuple(str(v) for v in multiple.get("default_choices") or ())
    else:
        single = model.get("single_answer") or {}
        answer_type = str(single.get("type") or "").removeprefix("QUESTION_ANSWER_TYPE_")
        choices = []
        default = (str(single["default_answer"]),) if "default_answer" in single else ()
    return Field(
        key=key,
        label=str(decoration.get("label") or key),
        step=step,
        step_title=step_title,
        widget=widget,
        answer_type=answer_type,
        mandatory="mandatory" in static,
        patterns=tuple(
            (str(r["value"]), str(r.get("error_message") or "invalid value"))
            for r in static.get("regex") or ()
        ),
        max_length=decoration.get("max_length"),
        exists_when=_requirements(rules.get("conditional_existence") or [], "existence_requirements"),
        choices=tuple(choices),
        default=default,
    )


def parse_form(config: dict[str, Any]) -> list[Field]:
    """Every question of the form, in the order a person meets them."""
    items = {i.get("identifier"): i for i in (config.get("definitions") or {}).get("items") or []}
    # An item may replace another one (`overrides`), e.g. tiles replacing an older dropdown.
    replacements = {i["overrides"]: i for i in items.values() if isinstance(i.get("overrides"), str)}
    fields = []
    for step in (config.get("navigation") or {}).get("ordered_steps") or []:
        for ref in step.get("ordered_items") or []:
            identifier = ref if isinstance(ref, str) else (ref or {}).get("identifier")
            item = replacements.get(identifier) or items.get(identifier)
            field = _field(item, step.get("identifier") or "", step.get("title") or "") if item else None
            if field:
                fields.append(field)
    if not fields:
        raise UnexpectedResponse("The ad form's description had no fields.")
    return fields


def normalize_value(field: Field, value: Any, answers: Answers) -> tuple[str, ...]:
    """Turn a client's value into the form's raw answer. Choices accept an id or a label."""
    values = value if isinstance(value, list | tuple) else [value]
    if field.answer_type == "BOOL":
        return tuple("true" if v in (True, "true", "oui", "Oui") else "false" for v in values)
    choices = field.choices_for(answers)
    raw = []
    for v in values:
        if isinstance(v, bool) and choices is not None:
            v = "Oui" if v else "Non"
        text = str(v).strip()
        if isinstance(v, float) and v.is_integer():
            text = str(int(v))
        if choices is not None and text not in choices:
            by_label = {label.casefold(): ident for ident, label in choices.items()}
            text = by_label.get(text.casefold(), text)
        raw.append(text)
    return tuple(raw)


@dataclass(frozen=True)
class Resolved:
    """The answers to type, field by field, once everything is valid."""

    fields: list[tuple[Field, tuple[str, ...]]]
    optional: list[Field]  # fields that apply but were left to their default


def describe(field: Field, answers: Answers) -> dict[str, Any]:
    info: dict[str, Any] = {"key": field.key, "label": field.label}
    choices = field.choices_for(answers)
    if choices is not None:
        info["choices"] = choices
        if field.multiple:
            info["multiple"] = True
    else:
        info["type"] = field.answer_type.lower()
    return info


def resolve(fields: list[Field], given: Mapping[str, Any], fixed: Answers) -> Resolved:
    """Check the client's values against the form and return what to type, in order.

    `given` are the client's values by field key. `fixed` are answers known otherwise (e.g. the
    address), needed for rules. Raises one ActionRefused listing every problem at once.
    """
    answers: dict[str, tuple[str, ...]] = dict(fixed)
    problems: list[str] = []
    used: set[str] = set()
    to_fill: list[tuple[Field, tuple[str, ...]]] = []
    optional: list[Field] = []
    for field in fields:
        if field.key in SPECIAL_KEYS or not field.exists(answers):
            continue
        if field.key in given:
            used.add(field.key)
            value = normalize_value(field, given[field.key], answers)
        else:
            value = field.default
        problems += check_value(field, value, answers)
        answers[field.key] = value
        if field.key in given:
            to_fill.append((field, value))
        elif not field.default:
            optional.append(field)
    for key in sorted(set(given) - used):
        known = {f.key for f in fields}
        problems.append(
            f"{key}: does not apply to this ad (check the other values)"
            if key in known
            else f"{key}: not a field of this form"
        )
    if problems:
        raise ActionRefused("The ad is not valid, nothing was done:\n- " + "\n- ".join(problems))
    return Resolved(to_fill, optional)


def check_value(field: Field, value: tuple[str, ...], answers: Answers) -> list[str]:
    name = f"{field.key} ({field.label})"
    if not value or not any(v.strip() for v in value):
        if field.mandatory:
            hint = describe(field, answers).get("choices")
            return [f"{name}: required" + (f", one of {hint}" if hint else "")]
        return []
    if not field.multiple and len(value) > 1:
        return [f"{name}: takes a single value"]
    choices = field.choices_for(answers)
    if choices is not None:
        wrong = [v for v in value if v not in choices]
        if wrong:
            return [f"{name}: {', '.join(wrong)} is not allowed; choose from {choices}"]
    problems = []
    for v in value:
        if field.max_length and len(v) > field.max_length:
            problems.append(f"{name}: at most {field.max_length} characters")
        for pattern, message in field.patterns:
            if not re.search(pattern, v):
                problems.append(f"{name}: {message}")
    return problems
