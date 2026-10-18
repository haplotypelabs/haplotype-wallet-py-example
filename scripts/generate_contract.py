"""Generate `src/wallet_example/contract.py` from `contract/connect-contract.json`.

    uv run scripts/generate_contract.py           # write contract.py
    uv run scripts/generate_contract.py --check   # fail if contract.py is out of date

`connect-contract.json` is published by Haplotype Wallet. The generated module is data
only: default URLs, scopes, the tools this app calls, and TypedDicts for those tools'
results, built from each tool's `output_schema`. To call another tool, add it to `TOOLS`
below and regenerate.
"""

import argparse
import difflib
import hashlib
import json
import keyword
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contract" / "connect-contract.json"
OUTPUT = ROOT / "src" / "wallet_example" / "contract.py"

SUPPORTED_CONTRACT_VERSION = 1
"""The contract shape this generator understands. The wallet bumps `contract_version`
only when the shape changes, and then this script needs updating by hand."""


@dataclass(frozen=True)
class Tool:
    constant: str
    """Prefix of the generated constants `<constant>_TOOL` and `<constant>_SCOPE`."""
    types: dict[str, str] = field(default_factory=dict[str, str])
    """Names of the TypedDicts in the output schema, keyed by path: "" is the whole
    result, `a.b` a nested property, `[]` the items of an array. Objects not listed
    are named after their parent class and property."""


TOOLS = {
    "list_samples": Tool(
        constant="LIST_SAMPLES",
        types={
            "": "ListSamplesResult",
            "result[]": "SampleRecord",
            "result[].provenance": "SampleProvenance",
            "result[].provenance.pipeline[]": "PipelineStep",
            "result[].provenance.source_upload": "SampleSourceUpload",
        },
    ),
}
"""The MCP tools this app calls."""

SCALARS = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "null": "None"}
STRUCTURAL = {"type", "anyOf", "properties", "required", "items"}
ANNOTATIONS = {"description", "title", "default", "examples", "additionalProperties"}


class ContractError(Exception):
    pass


def docstring(text: str, indent: str = "") -> list[str]:
    text = " ".join(text.split()).replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if text.endswith('"'):
        text = text[:-1] + '\\"'
    lines = textwrap.wrap(text, width=96 - len(indent)) or [""]
    if len(lines) == 1:
        return [f'{indent}"""{lines[0]}"""']
    return [f'{indent}"""{lines[0]}', *(indent + line for line in lines[1:]), f'{indent}"""']


def literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def pascal(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in name.split("_"))


class TypeEmitter:
    """Turns one tool's output schema into TypedDict classes, children first."""

    def __init__(self, tool: str, config: Tool, classes: dict[str, list[str]]) -> None:
        self.tool = tool
        self.names = config.types
        self.classes = classes
        self.used_paths: set[str] = set()
        self.uses_any = False

    def fail(self, path: str, message: str) -> ContractError:
        return ContractError(f"{self.tool} output_schema at {path or '<root>'}: {message}")

    def type_expr(self, node: object, path: str, fallback: str) -> str:
        if not isinstance(node, dict):
            raise self.fail(path, "expected a JSON Schema object")
        schema = cast(dict[str, Any], node)
        unknown = sorted(
            k for k in schema if k not in STRUCTURAL | ANNOTATIONS and not k.startswith("x-")
        )
        if unknown:
            raise self.fail(path, f"unsupported keywords {unknown}; extend the generator")
        if "anyOf" in schema:
            options = [self.type_expr(option, path, fallback) for option in schema["anyOf"]]
            return " | ".join(dict.fromkeys(options))
        kind = schema.get("type")
        if isinstance(kind, list):
            options = [
                self.type_expr({**schema, "type": k}, path, fallback)
                for k in cast(list[object], kind)
            ]
            return " | ".join(dict.fromkeys(options))
        if kind in SCALARS:
            return SCALARS[kind]
        if kind == "array":
            if "items" not in schema:
                self.uses_any = True
                return "list[Any]"
            return f"list[{self.type_expr(schema['items'], f'{path}[]', f'{fallback}Item')}]"
        if kind == "object" and "properties" in schema:
            return self.typed_dict(schema, path, fallback)
        if kind == "object" or kind is None:
            self.uses_any = True
            return "dict[str, Any]" if kind == "object" else "Any"
        raise self.fail(path, f"unsupported type {kind!r}")

    def typed_dict(self, schema: dict[str, Any], path: str, fallback: str) -> str:
        description = schema.get("description") or (
            f"Structured result of the `{self.tool}` tool." if path == "" else None
        )
        name = self.names.get(path, fallback)
        self.used_paths.add(path)
        if not name.isidentifier() or name in self.classes:
            raise self.fail(path, f"class name {name!r} is invalid or taken; name it in TOOLS")
        self.classes[name] = []  # reserve the name before the children take theirs
        properties: dict[str, Any] = schema["properties"]
        required = [p for p in schema.get("required", []) if p in properties]
        ordered = required + [p for p in properties if p not in required]
        body: list[str] = []
        if description:
            body += [*docstring(str(description), "    "), ""]
        for prop in ordered:
            if not prop.isidentifier() or keyword.iskeyword(prop):
                raise self.fail(path, f"property {prop!r} is not a Python identifier")
            prop_path = f"{path}.{prop}" if path else prop
            prop_schema = properties[prop]
            annotation = self.type_expr(prop_schema, prop_path, name + pascal(prop))
            if prop not in required:
                annotation = f"NotRequired[{annotation}]"
            body.append(f"    {prop}: {annotation}")
            if "description" in prop_schema:  # a dict: type_expr checked it
                body += docstring(str(prop_schema["description"]), "    ")
        del self.classes[name]  # re-insert after the children, so they are defined first
        self.classes[name] = [f"class {name}(TypedDict):", *(body or ["    pass"])]
        return name


def get(obj: object, key: str, kind: type, where: str) -> Any:
    value = cast(dict[str, Any], obj).get(key) if isinstance(obj, dict) else None
    if not isinstance(value, kind):
        raise ContractError(f"{where}.{key} is missing or not a {kind.__name__}")
    return value


def generate(raw: bytes) -> str:
    contract = json.loads(raw)
    version = get(contract, "contract_version", int, "contract")
    if version != SUPPORTED_CONTRACT_VERSION:
        raise ContractError(
            f"connect-contract.json has contract_version {version}, but this generator "
            f"understands version {SUPPORTED_CONTRACT_VERSION}. The contract's shape changed: "
            "update scripts/generate_contract.py for the new shape, then regenerate."
        )
    oauth = get(contract, "oauth", dict, "contract")
    mcp = get(contract, "mcp", dict, "contract")
    issuer = get(oauth, "issuer", str, "oauth")
    mcp_url = get(mcp, "url", str, "mcp")
    if get(oauth, "resource", str, "oauth") != mcp_url:
        raise ContractError(
            "oauth.resource differs from mcp.url. This app sends WALLET_MCP_URL as the "
            "OAuth resource, so it needs changing before it can use this contract."
        )
    scopes = get(contract, "scopes", list, "contract")
    tools = {get(t, "name", str, "tools[]"): t for t in get(contract, "tools", list, "contract")}

    out = [
        "# Generated by scripts/generate_contract.py from contract/connect-contract.json",
        f"# (contract_version {version}, sha256 {hashlib.sha256(raw).hexdigest()}).",
        "# Do not edit. Regenerate with `uv run scripts/generate_contract.py`.",
        '"""Everything this app takes from the Haplotype Wallet contract, in one place.',
        "",
        "Data only, no logic: endpoints, scopes, the tools this app calls, and the shape of",
        "their results. The rest of the app imports from here.",
        '"""',
        "",
        "from typing import {imports}",
        "",
        f"CONTRACT_VERSION = {version}",
        "",
        f"DEFAULT_ISSUER = {literal(issuer)}",
        '"""Production authorization server (WorkOS AuthKit)."""',
        "",
        f"AUTHORIZE_PATH = {literal(get(oauth, 'authorize_path', str, 'oauth'))}",
        '"""Authorize endpoint path under the issuer. The app reads it from OIDC discovery."""',
        "",
        f"TOKEN_PATH = {literal(get(oauth, 'token_path', str, 'oauth'))}",
        '"""Token endpoint path under the issuer. The app reads it from OIDC discovery."""',
        "",
        f"DEFAULT_MCP_URL = {literal(mcp_url)}",
        '"""Production MCP endpoint. Also the OAuth `resource` value."""',
        "",
        "ACCESS_TOKEN_LIFETIME_SECONDS = "
        + str(get(oauth, "access_token_lifetime_seconds", int, "oauth")),
        '"""How long an access token lasts. The token response field `expires_in` wins."""',
        "",
        "",
        "class Scope(NamedTuple):",
        '    """A wallet scope: what the consent screen shows, and the tools it unlocks."""',
        "",
        "    slug: str",
        "    name: str",
        "    description: str",
        "    tools: tuple[str, ...]",
        "",
        "",
        "SCOPES: tuple[Scope, ...] = (",
    ]
    for scope in scopes:
        scope_tools = [literal(t) for t in get(scope, "tools", list, "scopes[]")]
        out += [
            "    Scope(",
            f"        slug={literal(get(scope, 'slug', str, 'scopes[]'))},",
            f"        name={literal(get(scope, 'name', str, 'scopes[]'))},",
            f"        description={literal(get(scope, 'description', str, 'scopes[]'))},",
            f"        tools=({', '.join(scope_tools)}{',' if len(scope_tools) == 1 else ''}),",
            "    ),",
        ]
    out += [
        ")",
        '"""Wallet scopes, in consent-screen order."""',
        "",
        "SCOPE_DESCRIPTIONS = {",
        *(f"    {literal(s['slug'])}: {literal(s['description'])}," for s in scopes),
        "}",
        '"""Plain-words description of each scope, to show the user."""',
    ]

    if not isinstance(contract.get("errors"), dict):
        raise ContractError(
            "the contract has no `errors`, which this app needs to act on tool errors. It is "
            "older than the wallet release that added error codes."
        )
    errors = get(contract, "errors", dict, "contract")
    codes = get(errors, "codes", list, "errors")
    out += [
        "",
        f"ERROR_META_KEY = {literal(get(errors, 'meta_key', str, 'errors'))}",
        '"""A failed tool call carries its error in `result.meta[ERROR_META_KEY]`."""',
        "",
        "ERROR_RETRYABLE = {",
        *(
            f"    {literal(get(c, 'code', str, 'errors.codes[]'))}: "
            f"{get(c, 'retryable', bool, 'errors.codes[]')},"
            for c in codes
        ),
        "}",
        '"""Every error code the wallet sends, and whether retrying the same call can help."""',
    ]

    classes: dict[str, list[str]] = {}
    uses_any = False
    for name, config in TOOLS.items():
        if name not in tools:
            raise ContractError(f"the contract has no tool {name!r}, which this app calls")
        tool = tools[name]
        scope = get(tool, "scope", str, f"tools[{name}]")
        out += [
            "",
            f"{config.constant}_TOOL = {literal(name)}",
            f'"""MCP tool this app calls. Needs the `{scope}` scope."""',
            "",
            f"{config.constant}_SCOPE = {literal(scope)}",
        ]
        emitter = TypeEmitter(name, config, classes)
        output_schema = get(tool, "output_schema", dict, f"tools[{name}]")
        emitter.type_expr(output_schema, "", pascal(name) + "Result")
        if unused := sorted(set(config.types) - emitter.used_paths):
            raise ContractError(f"TOOLS names types at paths the {name} schema lacks: {unused}")
        uses_any = uses_any or emitter.uses_any
    for lines in classes.values():
        out += ["", "", *lines]

    imports = ["NamedTuple", "TypedDict"]
    source = "\n".join(out) + "\n"
    if "NotRequired[" in source:
        imports.append("NotRequired")
    if uses_any:
        imports.append("Any")
    return format_source(source.replace("{imports}", ", ".join(sorted(imports)), 1))


def format_source(source: str) -> str:
    """Run the output through ruff, so it matches `ruff format` exactly."""
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--stdin-filename", str(OUTPUT), "-"],
        input=source,
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise ContractError(f"ruff format failed on the generated code:\n{result.stderr}")
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if contract.py is stale")
    args = parser.parse_args()
    try:
        source = generate(CONTRACT.read_bytes())
    except (ContractError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    current = OUTPUT.read_text() if OUTPUT.exists() else ""
    relative = OUTPUT.relative_to(ROOT)
    if current == source:
        print(f"{relative} is up to date.")
        return 0
    if args.check:
        diff = difflib.unified_diff(
            current.splitlines(), source.splitlines(), "current", "generated", lineterm=""
        )
        print("\n".join(list(diff)[:40]))
        print(
            f"error: {relative} does not match contract/connect-contract.json. "
            "Run `uv run scripts/generate_contract.py`.",
            file=sys.stderr,
        )
        return 1
    OUTPUT.write_text(source)
    print(f"Wrote {relative}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
