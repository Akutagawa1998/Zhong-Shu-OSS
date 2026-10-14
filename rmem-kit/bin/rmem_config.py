"""Hub configuration: an optional hub-config.json merged over kit defaults.

A missing file means defaults. Keys that start with an underscore are comments.
Unknown keys are reported as warnings; an invalid value is an error that names
its key. Feature modules add their own key group with add_group().
"""
import copy
import json
import math
import os
from pathlib import Path
import stat
import zoneinfo

CONFIG_NAME = "hub-config.json"
CONFIG_MAX_BYTES = 256 * 1024
SCHEMA_VERSION = 1
DOC = ("rmem hub configuration. Every key is optional: a missing key uses the kit default "
       "shown here. Keys that start with an underscore are comments. Caps count characters, "
       "not bytes. See CONFIG.md for what each key does.")


class ConfigError(ValueError):
    """An invalid configuration; the message names the key and the accepted values."""


def integer(minimum, maximum=None):
    def check(value):
        if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
            return (f"must be an integer from {minimum} to {maximum}" if maximum is not None
                    else f"must be an integer of at least {minimum}")
        return ""
    return check


def number(minimum, maximum=None, above=False):
    def check(value):
        valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        if valid:
            valid = (value > minimum if above else value >= minimum) and (maximum is None or value <= maximum)
        if not valid:
            low = f"greater than {minimum}" if above else f"at least {minimum}"
            return f"must be a number {low}" + (f" and at most {maximum}" if maximum is not None else "")
        return ""
    return check


def time_zone(value):
    if value is None:
        return ""
    if not isinstance(value, str) or not value.strip():
        return "must be null (system time zone) or an IANA time zone name such as America/Los_Angeles"
    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return "must be null (system time zone) or an IANA time zone name such as America/Los_Angeles"
    return ""


def schema(value):
    return "" if value == SCHEMA_VERSION and type(value) is int else f"must be {SCHEMA_VERSION}; this kit reads schema version {SCHEMA_VERSION}"


def json_object(value):
    return "" if isinstance(value, dict) else "must be a JSON object"


DEFAULTS = {
    "schema_version": SCHEMA_VERSION,
    "caps": {"state_max_lines": 120, "state_max_chars": 8000,
             "thread_max_lines": 80, "thread_max_chars": 9000,
             "output_max_chars": 16000, "soft_ratio": 0.7},
    "display_tz": None,
    "roles": {},
    "leases": {"default_ttl_hours": 12},
    "requests": {"ack_warn_days": 2},
    "staleness": {"events_after_edit": 20},
    "git": {"uncommitted_warn_hours": 24},
    "storage": {"hub_warn_mb": 200, "file_warn_mb": 5},
}
# Dotted keys validate members of a key group; plain keys validate a whole value.
VALIDATORS = {
    "schema_version": schema,
    "caps.state_max_lines": integer(1),
    "caps.state_max_chars": integer(1),
    "caps.thread_max_lines": integer(1),
    "caps.thread_max_chars": integer(1),
    "caps.output_max_chars": integer(1000, 1000000),
    "caps.soft_ratio": number(0, 1, above=True),
    "display_tz": time_zone,
    "roles": json_object,
    "leases.default_ttl_hours": number(0, above=True),
    "requests.ack_warn_days": number(0),
    "staleness.events_after_edit": integer(1),
    "git.uncommitted_warn_hours": number(0, above=True),
    "storage.hub_warn_mb": number(0, above=True),
    "storage.file_warn_mb": number(0, above=True),
}
# Groups whose contents belong to a feature and are not checked key by key here.
FREE_FORM = {"roles"}


def add_group(name, defaults, validators, free_form=False):
    """Register a feature's key group: defaults is a dict, validators maps member name to check."""
    if name in DEFAULTS:
        raise ValueError(f"configuration group already registered: {name}")
    DEFAULTS[name] = copy.deepcopy(defaults)
    if free_form:
        FREE_FORM.add(name)
        VALIDATORS[name] = validators if callable(validators) else json_object
    else:
        for key, check in validators.items():
            VALIDATORS[f"{name}.{key}"] = check


def found(value):
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 80 else text[:66] + " … [clipped]"


def merge(raw):
    """Return (config, warnings) for a parsed hub-config.json object, or raise one ConfigError that
    lists every invalid key, so one round of fixes is enough."""
    config, warnings, errors = copy.deepcopy(DEFAULTS), [], []
    if raw is None:
        return config, warnings
    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_NAME} must contain one JSON object")
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        if key not in DEFAULTS:
            warnings.append(f"{CONFIG_NAME}: unknown key '{key}' is ignored; see CONFIG.md for known keys")
            continue
        if isinstance(DEFAULTS[key], dict) and key not in FREE_FORM:
            if not isinstance(value, dict):
                errors.append(f"{key} must be a JSON object (found {found(value)})")
                continue
            for name, item in value.items():
                dotted = f"{key}.{name}"
                if name.startswith("_"):
                    continue
                if dotted not in VALIDATORS:
                    warnings.append(f"{CONFIG_NAME}: unknown key '{dotted}' is ignored; see CONFIG.md for known keys")
                    continue
                problem = VALIDATORS[dotted](item)
                if problem:
                    errors.append(f"{dotted} {problem} (found {found(item)})")
                    continue
                config[key][name] = item
        else:
            problem = VALIDATORS[key](value)
            if problem:
                errors.append(f"{key} {problem} (found {found(value)})")
                continue
            config[key] = copy.deepcopy(value)
    if errors:
        raise ConfigError(f"{CONFIG_NAME}: " + "; ".join(errors))
    return config, warnings


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"{CONFIG_NAME}: duplicate key '{key}'")
        result[key] = value
    return result


def read(hub):
    """Parse <hub>/hub-config.json; None when the file is absent."""
    path = Path(hub) / CONFIG_NAME
    if path.is_symlink():
        raise ConfigError(f"{CONFIG_NAME} must be a regular file, not a symlink")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ConfigError(f"{CONFIG_NAME} must be a regular file")
        data = stream.read(CONFIG_MAX_BYTES + 1)
    if len(data) > CONFIG_MAX_BYTES:
        raise ConfigError(f"{CONFIG_NAME} exceeds {CONFIG_MAX_BYTES} bytes")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=unique_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{CONFIG_NAME} is not valid JSON: {exc}") from exc


def load(hub, environ=None):
    """Return (config, warnings). The RMEM_TZ environment variable overrides display_tz."""
    environ = os.environ if environ is None else environ
    config, warnings = merge(read(hub))
    if environ.get("RMEM_TZ"):
        if time_zone(environ["RMEM_TZ"]):
            raise ConfigError("RMEM_TZ must be an IANA time zone name such as America/Los_Angeles")
        config["display_tz"] = environ["RMEM_TZ"]
    return config, warnings


def defaults():
    return copy.deepcopy(DEFAULTS)


def template():
    """The commented default copy that init writes as hub-config.json."""
    return {"_doc": DOC, **defaults()}


def template_text():
    return json.dumps(template(), ensure_ascii=False, indent=2) + "\n"


def zone(config):
    """ZoneInfo for display, or None for the system zone."""
    name = (config or {}).get("display_tz")
    return zoneinfo.ZoneInfo(name) if name else None
