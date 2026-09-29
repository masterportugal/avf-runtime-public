"""Autonomous Venture Factory core domain package."""

from .models import (
    Evidence,
    Marketplace,
    Opportunity,
    OpportunityStage,
    Source,
    Track,
)
from .scoring import GateResult, OpportunityScorer, ScoreBreakdown

__all__ = [
    "Evidence",
    "Marketplace",
    "Opportunity",
    "OpportunityStage",
    "Source",
    "Track",
    "GateResult",
    "OpportunityScorer",
    "ScoreBreakdown",
]

from .product_pipeline import (
    BuildSandbox,
    ProductBuildPlan,
    ProductComposer,
    ProductSpec,
    ProductSpecGenerator,
    RepositoryComponent,
)

__all__ += [
    "BuildSandbox", "ProductBuildPlan", "ProductComposer", "ProductSpec",
    "ProductSpecGenerator", "RepositoryComponent",
]

__version__ = "0.38.0"
