"""Provider-native boundaries for Bear Hug's normalized hook runtime."""

from bearhug.hook_adapters._common import (
    AdapterCustody,
    HookAdapterError,
    UnsupportedProviderDecision,
    to_dispatch_custody,
    verify_adapter_custody,
)
from bearhug.hook_adapters.claude import (
    AdaptedClaudeAskUserQuestion,
    adapt_claude_ask_user_question,
    render_claude_ask_user_question_result,
)
from bearhug.hook_adapters.codex import (
    AdaptedCodexUserInput,
    adapt_codex_user_input_request,
    render_codex_user_input_result,
)
from bearhug.hook_adapters.lifecycle import (
    CLAUDE_LIFECYCLE_VERSION,
    CODEX_LIFECYCLE_VERSION,
    AdaptedLifecycleHook,
    adapt_lifecycle_hook,
)
from bearhug.hook_adapters.tool_use import (
    CODEX_TOOL_ADAPTER,
    CODEX_TOOL_PROVIDER_VERSION,
    AdaptedCodexApplyPatch,
    PatchEffect,
    adapt_codex_apply_patch,
    render_codex_apply_patch_result,
)

__all__ = [
    "AdaptedClaudeAskUserQuestion",
    "AdaptedCodexUserInput",
    "AdaptedCodexApplyPatch",
    "AdaptedLifecycleHook",
    "AdapterCustody",
    "HookAdapterError",
    "UnsupportedProviderDecision",
    "CLAUDE_LIFECYCLE_VERSION",
    "CODEX_LIFECYCLE_VERSION",
    "CODEX_TOOL_ADAPTER",
    "CODEX_TOOL_PROVIDER_VERSION",
    "PatchEffect",
    "adapt_claude_ask_user_question",
    "adapt_codex_user_input_request",
    "adapt_codex_apply_patch",
    "adapt_lifecycle_hook",
    "render_codex_apply_patch_result",
    "render_claude_ask_user_question_result",
    "render_codex_user_input_result",
    "to_dispatch_custody",
    "verify_adapter_custody",
]
