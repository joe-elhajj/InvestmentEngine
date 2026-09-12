"""DCF configuration validation shared by file loading and engine entry points."""

import math
from pathlib import Path

import yaml


def validate_dcf_config(config: dict, *, require_growth: bool = True) -> None:
    """Validate supplied DCF bundles; omitted/empty DCF retains existing defaults.

    Reverse DCF solves for growth, so its direct callers may omit fcf_growth.
    No economic ranges or scenario ordering constraints are imposed here.
    """
    def mapping(value, path):
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be a mapping")
        return value

    def keys(value, required, allowed, path):
        missing = sorted(required - value.keys())
        unknown = sorted(map(repr, value.keys() - allowed))
        if missing or unknown:
            raise ValueError(f"{path}: missing keys {missing}; unexpected keys {unknown}")

    def number(value, path):
        try:
            finite = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError(f"{path} must be a finite number (not boolean)")

    mapping(config, "config")
    val = mapping(config.get("valuation", {}), "valuation")
    dcf = mapping(val.get("dcf", {}), "valuation.dcf")
    if not dcf:
        return
    path = "valuation.dcf"
    keys(dcf, {"scenarios"}, {"scenarios", "projection_years", "sensitivity"}, path)
    n = dcf.get("projection_years", 5)
    if type(n) is not int or n < 1:
        raise ValueError(f"{path}.projection_years must be a positive integer")
    scenarios = mapping(dcf["scenarios"], f"{path}.scenarios")
    keys(scenarios, {"bear", "base", "bull"}, {"bear", "base", "bull"}, f"{path}.scenarios")
    for name in ("bear", "base", "bull"):
        sp = f"{path}.scenarios.{name}"
        sc = mapping(scenarios[name], sp)
        required = {"wacc", "terminal_growth"} | ({"fcf_growth"} if require_growth else set())
        keys(sc, required, {"wacc", "terminal_growth", "fcf_growth"}, sp)
        for field in ("wacc", "terminal_growth"):
            number(sc[field], f"{sp}.{field}")
        if "fcf_growth" in sc:
            growth = sc["fcf_growth"]
            if isinstance(growth, list):
                if not growth:
                    raise ValueError(f"{sp}.fcf_growth must not be empty")
                for i, value in enumerate(growth):
                    number(value, f"{sp}.fcf_growth[{i}]")
            else:
                number(growth, f"{sp}.fcf_growth")
    if "sensitivity" in dcf:
        sens = mapping(dcf["sensitivity"], f"{path}.sensitivity")
        keys(sens, set(), {"wacc", "terminal_growth"}, f"{path}.sensitivity")
        for field, values in sens.items():
            if not isinstance(values, list):
                raise ValueError(f"{path}.sensitivity.{field} must be a list")
            for i, value in enumerate(values):
                number(value, f"{path}.sensitivity.{field}[{i}]")


def load_config(path) -> dict:
    """Reject duplicate DCF YAML keys before safe_load can discard them."""
    text = Path(path).read_text()

    def check(node, path=(), ancestors=()):
        if not isinstance(node, yaml.MappingNode):
            return
        if id(node) in ancestors:
            raise ValueError(f"{'.'.join(path)}: recursive YAML mapping")
        seen = set()
        for key, value in node.value:
            if not isinstance(key, yaml.ScalarNode):
                raise ValueError(f"{'.'.join(path) or 'config'}: mapping key must be scalar")
            name = key.value
            relevant = path[:2] == ("valuation", "dcf") or (path, name) in (
                ((), "valuation"), (("valuation",), "dcf"),
            )
            if relevant:
                if name in seen:
                    raise ValueError(f"{'.'.join((*path, name))}: duplicate YAML key")
                seen.add(name)
                check(value, (*path, name), (*ancestors, id(node)))

    check(yaml.compose(text))
    config = yaml.safe_load(text)
    validate_dcf_config(config)
    return config
