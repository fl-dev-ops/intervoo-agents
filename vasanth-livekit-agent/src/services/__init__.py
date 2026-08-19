"""Application services layer.

This package contains application services that orchestrate domain logic:
- agent: Agent management, avatar providers
- identity: User identity resolution
- simulation: Simulation mode support
- chroma: Vector database operations
"""

from services.agent.unified import UnifiedAgent
from services.chroma.repository import (
    chroma_configured,
    chroma_runtime_identity,
    get_cached_collection,
    prewarm_chroma,
)
from services.identity.resolver import (
    resolve_phone_number_from_call_context,
    resolve_user_id_from_room_metadata,
)
from services.simulation.shims import (
    install_answer_submit_shim,
    merge_simulation_userdata,
)

__all__ = [
    "UnifiedAgent",
    "chroma_configured",
    "chroma_runtime_identity",
    "get_cached_collection",
    "install_answer_submit_shim",
    "merge_simulation_userdata",
    "prewarm_chroma",
    "resolve_phone_number_from_call_context",
    "resolve_user_id_from_room_metadata",
]
