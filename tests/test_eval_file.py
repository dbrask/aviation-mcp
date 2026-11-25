"""The tool-selection eval seed (evals/tool_selection.yaml, from docs/PLAN.md) must stay in
sync with the tools: every expected call names a real tool and its arguments validate
against that tool's input schema. Running it against a live model is a later step."""

from __future__ import annotations

from pathlib import Path

import jsonschema
import pytest
import yaml
from mcp import Client

EVAL = Path(__file__).parent.parent / "evals" / "tool_selection.yaml"


def cases():
    return yaml.safe_load(EVAL.read_text())["cases"]


def test_has_the_ten_prompts_from_the_plan():
    data = cases()
    assert [c["id"] for c in data] == list(range(1, 11))
    assert all(c["prompt"].strip() for c in data)


@pytest.mark.parametrize("case", cases(), ids=lambda c: f"case{c['id']}")
async def test_expected_calls_are_valid_tool_calls(server, case):
    async with Client(server) as client:
        schemas = {t.name: t.input_schema for t in (await client.list_tools()).tools}
    accept = case["expect"]
    assert accept, "each case lists at least one acceptable outcome"
    for outcome in accept:
        if outcome.get("no_call"):
            continue
        assert outcome["tool"] in schemas
        args = {k: v for k, v in outcome.get("args", {}).items() if not str(v).startswith("<")}
        jsonschema.validate(args, schemas[outcome["tool"]])


# Airport names used in the prompts -> ICAO code. A schema check accepts any 4 letters, so
# this pins the meaning. Kiruna was once ESKN (Stockholm/Skavsta); AWC stationinfo lists
# ESNQ as Kiruna and ESKN as Stockholm/Skavsta.
AIRPORTS = {
    "Arlanda": "ESSA",
    "Kiruna": "ESNQ",
    "Heathrow": "EGLL",
    "Schiphol": "EHAM",
    "Landvetter": "ESGG",
    "Bromma": "ESSB",
    "Frankfurt": "EDDF",
    "Copenhagen": "EKCH",
}
PLAN = Path(__file__).parent.parent / "docs" / "PLAN.md"


def expected_codes(case) -> set[str]:
    codes: set[str] = set()
    for outcome in case["expect"]:
        args = outcome.get("args", {})
        codes.update(args.get("stations", []))
        if "airport" in args:
            codes.add(args["airport"])
    return codes


@pytest.mark.parametrize("case", cases(), ids=lambda c: f"case{c['id']}")
def test_expected_airports_match_the_names_in_the_prompt(case):
    named = {code for name, code in AIRPORTS.items() if name in case["prompt"]}
    assert expected_codes(case) == named


def test_kiruna_is_esnq_in_the_seed_and_the_plan():
    [kiruna] = [c for c in cases() if "Kiruna" in c["prompt"]]
    assert expected_codes(kiruna) == {"ESNQ"}
    [row] = [line for line in PLAN.read_text().splitlines() if "Kiruna" in line]
    assert '"ESNQ"' in row and "ESKN" not in row


@pytest.mark.parametrize("case", cases(), ids=lambda c: f"case{c['id']}")
def test_plan_table_names_the_same_airports_as_the_seed(case):
    rows = [
        line for line in PLAN.read_text().splitlines() if line.startswith(f"| {case['id']} |")
    ]
    assert len(rows) == 1
    in_row = {code for code in AIRPORTS.values() if f'"{code}"' in rows[0]}
    # Case 10's row is prose ("no call, or ..."); every row that names codes must agree.
    assert in_row == expected_codes(case) or (case["id"] == 10 and not in_row)
