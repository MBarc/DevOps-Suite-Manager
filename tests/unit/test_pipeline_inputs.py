import pytest

from dosm.pipelines.inputs import (
    InputValidationError,
    coerce_for_github,
    coerce_for_octopus,
    coerce_run_inputs,
    normalize_schema,
    parse_schema_form,
    split_azure_devops_inputs,
)

# ── parse_schema_form ────────────────────────────────────────────────────────


def test_parse_schema_form_minimal_string():
    form = {"schema_name__0": "version", "schema_type__0": "string"}
    assert parse_schema_form(form) == [{"name": "version", "type": "string"}]


def test_parse_schema_form_choice_with_options():
    form = {
        "schema_name__0": "environment",
        "schema_type__0": "choice",
        "schema_options__0": "dev, staging,prod",
        "schema_default__0": "staging",
        "schema_required__0": "1",
        "schema_desc__0": "Target env",
    }
    rows = parse_schema_form(form)
    assert rows == [
        {
            "name": "environment",
            "type": "choice",
            "options": ["dev", "staging", "prod"],
            "default": "staging",
            "required": True,
            "description": "Target env",
        }
    ]


def test_parse_schema_form_skips_blank_name_rows():
    form = {
        "schema_name__0": "version",
        "schema_type__0": "string",
        "schema_name__1": "",  # blank trailing row from the JS editor
        "schema_type__1": "boolean",
    }
    assert len(parse_schema_form(form)) == 1


def test_parse_schema_form_invalid_type_falls_back_to_string():
    form = {"schema_name__0": "x", "schema_type__0": "garbage"}
    assert parse_schema_form(form)[0]["type"] == "string"


def test_parse_schema_form_options_only_applied_for_choice():
    form = {
        "schema_name__0": "version",
        "schema_type__0": "string",
        "schema_options__0": "1.0,2.0",
    }
    row = parse_schema_form(form)[0]
    assert "options" not in row


# ── normalize_schema ─────────────────────────────────────────────────────────


def test_normalize_schema_fills_missing_type():
    out = normalize_schema([{"name": "x"}])
    assert out == [{"name": "x", "type": "string"}]


def test_normalize_schema_drops_empty_names_and_non_dicts():
    out = normalize_schema([{"name": ""}, "garbage", None, {"name": "ok"}])
    assert out == [{"name": "ok", "type": "string"}]


# ── coerce_run_inputs ────────────────────────────────────────────────────────


def test_coerce_run_inputs_string():
    schema = [{"name": "version", "type": "string"}]
    assert coerce_run_inputs(schema, {"input__version": "1.2.3"}) == {"version": "1.2.3"}


def test_coerce_run_inputs_boolean_checked_and_unchecked():
    schema = [{"name": "dry_run", "type": "boolean"}]
    assert coerce_run_inputs(schema, {"input__dry_run": "1"}) == {"dry_run": True}
    assert coerce_run_inputs(schema, {}) == {"dry_run": False}


def test_coerce_run_inputs_choice_validates_options():
    schema = [{"name": "env", "type": "choice", "options": ["dev", "prod"]}]
    assert coerce_run_inputs(schema, {"input__env": "prod"}) == {"env": "prod"}
    with pytest.raises(InputValidationError):
        coerce_run_inputs(schema, {"input__env": "staging"})


def test_coerce_run_inputs_number_int_vs_float():
    schema = [{"name": "n", "type": "number"}]
    assert coerce_run_inputs(schema, {"input__n": "42"}) == {"n": 42}
    assert coerce_run_inputs(schema, {"input__n": "1.5"}) == {"n": 1.5}
    with pytest.raises(InputValidationError):
        coerce_run_inputs(schema, {"input__n": "not-a-number"})


def test_coerce_run_inputs_required_missing_raises():
    schema = [{"name": "version", "type": "string", "required": True}]
    with pytest.raises(InputValidationError, match="version"):
        coerce_run_inputs(schema, {})


def test_coerce_run_inputs_optional_missing_omits_key():
    """Empty optional inputs should be dropped so workflow defaults apply."""
    schema = [{"name": "version", "type": "string"}]
    assert coerce_run_inputs(schema, {}) == {}


# ── coerce_for_github ────────────────────────────────────────────────────────


def test_coerce_for_github_stringifies_everything():
    out = coerce_for_github({"env": "prod", "dry_run": True, "force": False, "n": 7, "f": 1.5})
    assert out == {"env": "prod", "dry_run": "true", "force": "false", "n": "7", "f": "1.5"}


def test_coerce_for_github_drops_none():
    assert coerce_for_github({"env": "prod", "skip": None}) == {"env": "prod"}


# ── coerce_for_octopus ──────────────────────────────────────────────────────


def test_coerce_for_octopus_stringifies_with_lowercase_bools():
    out = coerce_for_octopus({"env": "prod", "force": True, "skip": False, "n": 7})
    assert out == {"env": "prod", "force": "true", "skip": "false", "n": "7"}


def test_coerce_for_octopus_drops_none():
    assert coerce_for_octopus({"env": "prod", "x": None}) == {"env": "prod"}


# ── split_azure_devops_inputs ───────────────────────────────────────────────


def test_split_ado_default_bucket_is_template_params():
    tp, vars_ = split_azure_devops_inputs({"env": "prod", "version": "1.2.3"})
    assert tp == {"env": "prod", "version": "1.2.3"}
    assert vars_ == {}


def test_split_ado_var_prefix_routes_to_variables():
    tp, vars_ = split_azure_devops_inputs(
        {"env": "prod", "var.deploy_env": "blue", "var.flag": True}
    )
    assert tp == {"env": "prod"}
    assert vars_ == {"deploy_env": "blue", "flag": "true"}


def test_split_ado_template_params_keep_native_types():
    """ADO runtime parameters are typed - booleans/numbers go on the wire as
    native JSON. Only the variables half is string-coerced."""
    tp, vars_ = split_azure_devops_inputs({"force": True, "count": 7, "ratio": 1.5})
    assert tp == {"force": True, "count": 7, "ratio": 1.5}
    assert vars_ == {}


def test_split_ado_variables_are_string_coerced():
    _tp, vars_ = split_azure_devops_inputs(
        {"var.bool": False, "var.num": 42, "var.flt": 1.5}
    )
    assert vars_ == {"bool": "false", "num": "42", "flt": "1.5"}


def test_split_ado_drops_none_in_both_buckets():
    tp, vars_ = split_azure_devops_inputs({"x": None, "var.y": None, "ok": "v"})
    assert tp == {"ok": "v"}
    assert vars_ == {}
