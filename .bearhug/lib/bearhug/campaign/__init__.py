"""Read-only campaign inputs and validation.

The package deliberately starts with corpus metadata only. It does not schedule work, launch an
agent, mutate another repository, or infer which project document is authoritative.
"""

from bearhug.campaign.author import (
    CampaignAuthorError,
    CampaignAuthorResult,
    run_campaign_author,
    validate_campaign_author_receipt,
    write_campaign_author_receipt,
)
from bearhug.campaign.capsule_candidate import capture_capsule_candidate
from bearhug.campaign.capsule_packets import (
    CapsulePacketError,
    PacketSource,
    RenderedExecutionPacket,
    RenderedSource,
    render_execution_packet,
)
from bearhug.campaign.capsule_runtime import (
    CapsuleEpisodeRuntime,
    CapsuleRuntime,
    CapsuleRuntimeError,
    CapsuleRuntimeRecoveryError,
    EpisodeOutcome,
    ExecutionCapsuleRuntime,
)
from bearhug.campaign.claims import (
    CampaignClaimConflict,
    CampaignClaimError,
    acquire_claim,
    active_claims,
    build_claim,
    release_claim,
    validate_claim_record,
)
from bearhug.campaign.contracts import (
    CANONICAL_ALGORITHM as CAMPAIGN_CONTRACT_CANONICAL_ALGORITHM,
)
from bearhug.campaign.contracts import (
    CampaignContractError,
    CampaignContractResult,
    ContractValidationResult,
    canonical_campaign_bytes,
    validate_campaign_run,
    validate_campaign_template,
    validate_programme_campaign_index,
    validate_work_unit,
)
from bearhug.campaign.corpus import (
    CANONICAL_ALGORITHM,
    CorpusDiagnostic,
    CorpusManifestError,
    CorpusValidationResult,
    canonical_manifest_bytes,
    load_manifest,
    validate_manifest,
)
from bearhug.campaign.importer import (
    CampaignImportError,
    CampaignTypeset,
    SubjectIdentity,
    import_campaign_typeset,
    inspect_subject,
    revalidate_campaign_typeset,
)
from bearhug.campaign.integration import (
    CampaignIntegrationError,
    CampaignIntegrationResult,
    CampaignIntegrationStore,
    integrate_campaign_candidates,
    integrate_reviewed_campaign_candidates,
    validate_integration_receipt,
)
from bearhug.campaign.review import (
    CampaignReviewError,
    CampaignReviewStore,
    build_review_receipt,
    canonical_json_sha256,
    validate_review_receipt,
    worktree_sha256,
)
from bearhug.campaign.reviewer import (
    CampaignReviewerError,
    CampaignReviewerResult,
    run_campaign_reviewer,
)

__all__ = [
    "CANONICAL_ALGORITHM",
    "CAMPAIGN_CONTRACT_CANONICAL_ALGORITHM",
    "CampaignAuthorError",
    "CampaignAuthorResult",
    "CapsuleEpisodeRuntime",
    "CapsulePacketError",
    "CapsuleRuntime",
    "CapsuleRuntimeError",
    "CapsuleRuntimeRecoveryError",
    "CampaignClaimConflict",
    "CampaignClaimError",
    "CampaignContractError",
    "CampaignContractResult",
    "CampaignImportError",
    "CampaignIntegrationError",
    "CampaignIntegrationResult",
    "CampaignIntegrationStore",
    "CampaignReviewError",
    "CampaignReviewStore",
    "CampaignReviewerError",
    "CampaignReviewerResult",
    "CampaignTypeset",
    "EpisodeOutcome",
    "ExecutionCapsuleRuntime",
    "CorpusDiagnostic",
    "CorpusManifestError",
    "CorpusValidationResult",
    "PacketSource",
    "RenderedExecutionPacket",
    "RenderedSource",
    "SubjectIdentity",
    "ContractValidationResult",
    "canonical_campaign_bytes",
    "canonical_manifest_bytes",
    "capture_capsule_candidate",
    "acquire_claim",
    "active_claims",
    "build_claim",
    "build_review_receipt",
    "canonical_json_sha256",
    "import_campaign_typeset",
    "integrate_campaign_candidates",
    "integrate_reviewed_campaign_candidates",
    "inspect_subject",
    "revalidate_campaign_typeset",
    "load_manifest",
    "release_claim",
    "render_execution_packet",
    "run_campaign_author",
    "run_campaign_reviewer",
    "validate_campaign_author_receipt",
    "validate_campaign_run",
    "validate_campaign_template",
    "validate_claim_record",
    "validate_integration_receipt",
    "validate_manifest",
    "validate_programme_campaign_index",
    "validate_review_receipt",
    "validate_work_unit",
    "worktree_sha256",
    "write_campaign_author_receipt",
]
