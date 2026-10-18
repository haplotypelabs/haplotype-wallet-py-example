"""Everything this app takes from the Haplotype Wallet contract, in one place.

Data only, no logic: endpoints, scopes, the tool this app calls, and the shape of its
result. The rest of the app imports from here. Keep it that way, so this file can be
replaced as a whole when the contract changes.
"""

from typing import NotRequired, TypedDict

DEFAULT_ISSUER = "https://authkit.haplotype.ai"
"""Production authorization server (WorkOS AuthKit)."""

DEFAULT_MCP_URL = "https://api.haplotype.ai/mcp"
"""Production MCP endpoint. Also the OAuth `resource` value."""

SCOPE_DESCRIPTIONS = {
    "samples:read": "See your samples and how they were produced",
    "variants:read": "Read your genotype at the positions it asks about",
    "scores:read": "Read your polygenic scores and analysis runs",
    "runs:write": "Start new analyses on your genotypes",
}
"""Wallet scopes, with plain-words descriptions to show the user."""

LIST_SAMPLES_TOOL = "list_samples"
"""MCP tool that lists the user's samples. Needs the `samples:read` scope."""

LIST_SAMPLES_SCOPE = "samples:read"


class SampleSourceUpload(TypedDict):
    filename: str
    uploaded_at: str


class PipelineStep(TypedDict):
    step: str | None
    tools: list[str]
    started_at: str | None
    finished_at: str | None


class SampleProvenance(TypedDict):
    """How a sample was produced, and the uploaded file it came from."""

    is_imputed: bool | None
    impute_reason: str | None
    imputation_tool: str | None
    reference_panel: str | None
    source_build: str | None
    target_build: str | None
    variant_count: int | None
    processed_at: str | None
    pipeline: list[PipelineStep]
    source_upload: SampleSourceUpload


class SampleRecord(TypedDict):
    """One item of the `list_samples` result."""

    sample_id: str
    label: str
    source_filename: str
    file_size: int
    processed_at: str
    provenance: SampleProvenance | None
    haplohub_created: NotRequired[str | None]
