"""
Data models for the diagnostic interview assessment backend.

Structures for questions, rubrics, responses, and scoring.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Question:
    """A single assessment question."""
    id: str
    text: str
    question_type: str  # "Language", "Confidence", or "Thinking"
    category: str  # "opening", "closing", "domain", or "behavioral"
    difficulty_level: str  # "easy", "medium", or "hard"


@dataclass
class RubricLevel:
    """A single level within a rubric (e.g., TF1, VCP2, A2)."""
    level_name: str  # e.g., "TF1", "VCP3", "A2"
    label: str  # e.g., "Not shown", "Emerging", "Waystage"
    score_range: tuple  # (min, max) e.g., (0, 24)
    descriptors: Dict[str, str]  # dimension/signal name -> descriptor text


@dataclass
class Rubric:
    """Complete rubric for a dimension (Thinking, Confidence, or Language)."""
    framework: str  # "TF 1-4", "VCP 1-4", or "CEFR"
    dimension_name: str  # e.g., "Thinking", "Confidence", "Language"
    levels: Dict[str, RubricLevel]  # level_name -> RubricLevel
    scoring_note: str  # e.g., "Holistic across 4 dimensions"
    sub_dimensions: Optional[List[str]] = None  # e.g., ["Fluency", "Accuracy", "Range", "Coherence"]


@dataclass
class QuestionResponse:
    """Scored response for a single question."""
    question_id: str
    thinking_level: str  # "TF1", "TF2", "TF3", or "TF4"
    confidence_level: str  # "VCP1", "VCP2", "VCP3", or "VCP4"
    language_levels: Dict[str, str]  # {"Fluency": "A2", "Accuracy": "B1", "Range": "A2", "Coherence": "A2"}

    def get_thinking_score(self) -> float:
        """Map TF level to numeric score (midpoint of range)."""
        mapping = {
            "TF1": 12,   # midpoint of 0-24
            "TF2": 37,   # midpoint of 25-49
            "TF3": 62,   # midpoint of 50-74
            "TF4": 87,   # midpoint of 75-100
        }
        return mapping.get(self.thinking_level, 0)

    def get_confidence_score(self) -> float:
        """Map VCP level to numeric score (midpoint of range)."""
        mapping = {
            "VCP1": 12,  # midpoint of 0-24
            "VCP2": 37,  # midpoint of 25-49
            "VCP3": 62,  # midpoint of 50-74
            "VCP4": 87,  # midpoint of 75-100
        }
        return mapping.get(self.confidence_level, 0)

    def get_language_score(self) -> float:
        """
        Map CEFR levels to numeric scores and average across dimensions.
        Each dimension independently scored; no flattening.
        """
        cefr_mapping = {
            "Pre-A1": 12,  # midpoint of 0-24
            "A1": 12,      # midpoint of 0-24
            "A2": 37,      # midpoint of 25-49
            "B1": 37,      # midpoint of 25-49
            "B2": 62,      # midpoint of 50-74
            "C1": 62,      # midpoint of 50-74
            "C2": 87,      # midpoint of 75-100
        }
        if not self.language_levels:
            return 0

        scores = [
            cefr_mapping.get(level, 0)
            for level in self.language_levels.values()
        ]
        return sum(scores) / len(scores) if scores else 0

    def get_question_score(self) -> float:
        """Average TF, VCP, and Language scores for this question."""
        tf_score = self.get_thinking_score()
        vcp_score = self.get_confidence_score()
        lang_score = self.get_language_score()
        return (tf_score + vcp_score + lang_score) / 3


@dataclass
class AssessmentResult:
    """Final assessment result with score and recommendations."""
    total_score: float  # 0-100
    thinking_avg: float
    confidence_avg: float
    language_avg: float
    salary_lpa: float  # e.g., 25.5 (in LPA per annum)
    salary_band: str  # e.g., "10-40 LPA"
    salary_percentile: float  # e.g., 0.75 (75th percentile)
    question_responses: List[QuestionResponse] = field(default_factory=list)
