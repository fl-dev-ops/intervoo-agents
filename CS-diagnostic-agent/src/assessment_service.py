"""
Assessment Service Layer for the diagnostic interview agent.

Orchestrates:
- Starting assessment sessions
- Collecting user responses
- Evaluating responses against rubrics
- Calculating final scores and recommendations
"""

import uuid
from typing import List, Dict, Optional
from assessment_models import Question, QuestionResponse, AssessmentResult
from assessment_data import (
    DUMMY_QUESTIONS,
    THINKING_RUBRIC,
    CONFIDENCE_RUBRIC,
    LANGUAGE_RUBRIC,
)
from scoring import ScoringEngine, RecommendationEngine


class AssessmentService:
    """Orchestrates the diagnostic interview assessment flow."""

    def __init__(self):
        """Initialize a new assessment service instance."""
        self.session_id: Optional[str] = None
        self.questions: List[Question] = DUMMY_QUESTIONS
        self.responses: Dict[str, QuestionResponse] = {}
        self.assessment_result: Optional[AssessmentResult] = None

    def start_assessment(self) -> List[Question]:
        """
        Initialize a new assessment session.

        Returns:
            List of Question objects to ask
        """
        self.session_id = str(uuid.uuid4())
        self.responses = {}
        self.assessment_result = None
        return self.questions

    def submit_response(
        self,
        question_id: str,
        raw_response: str,
    ) -> QuestionResponse:
        """
        Submit a user's response to a question.

        Evaluates the raw response against rubrics to assign levels,
        then stores the scored response.

        Args:
            question_id: ID of the question being answered
            raw_response: User's raw response (text/transcript)

        Returns:
            QuestionResponse with evaluated levels and scores
        """
        # Validate question exists
        question = self._get_question(question_id)
        if not question:
            raise ValueError(f"Question {question_id} not found")

        # Evaluate response against rubrics
        thinking_level = self._evaluate_thinking(question_id, raw_response)
        confidence_level = self._evaluate_confidence(question_id, raw_response)
        language_levels = self._evaluate_language(question_id, raw_response)

        # Create and store QuestionResponse
        response = QuestionResponse(
            question_id=question_id,
            thinking_level=thinking_level,
            confidence_level=confidence_level,
            language_levels=language_levels,
        )
        self.responses[question_id] = response

        return response

    def _get_question(self, question_id: str) -> Optional[Question]:
        """Find a question by ID."""
        return next((q for q in self.questions if q.id == question_id), None)

    def _evaluate_thinking(self, question_id: str, raw_response: str) -> str:
        """
        Evaluate thinking level from response.

        For V1: Returns mock level based on question difficulty.
        TODO: Replace with actual evaluation (human scorer or LLM evaluator).

        Args:
            question_id: Question being answered
            raw_response: User's response text

        Returns:
            TF level (TF1, TF2, TF3, or TF4)
        """
        question = self._get_question(question_id)
        if not question:
            return "TF2"

        # Mock: Map difficulty to level
        # This is placeholder - will be replaced with real evaluation
        difficulty_mapping = {
            "easy": "TF3",
            "medium": "TF2",
            "hard": "TF2",
        }
        return difficulty_mapping.get(question.difficulty_level, "TF2")

    def _evaluate_confidence(self, question_id: str, raw_response: str) -> str:
        """
        Evaluate confidence level from response.

        For V1: Returns mock level.
        TODO: Extract from audio signals (volume, pace, pause, latency) or use LLM.

        Args:
            question_id: Question being answered
            raw_response: User's response text

        Returns:
            VCP level (VCP1, VCP2, VCP3, or VCP4)
        """
        # Mock: Return consistent level
        # This is placeholder - will be replaced with actual audio analysis or LLM evaluation
        return "VCP3"

    def _evaluate_language(
        self, question_id: str, raw_response: str
    ) -> Dict[str, str]:
        """
        Evaluate language levels from response.

        For V1: Returns mock levels for each dimension.
        TODO: Analyze text against CEFR criteria or use LLM evaluator.

        Args:
            question_id: Question being answered
            raw_response: User's response text

        Returns:
            Dict mapping dimension -> CEFR level
            e.g., {"Fluency": "B1", "Accuracy": "A2", "Range": "B1", "Coherence": "B1"}
        """
        # Mock: Return consistent levels
        # This is placeholder - will be replaced with actual text analysis or LLM evaluation
        return {
            "Fluency": "B1",
            "Accuracy": "A2",
            "Range": "B1",
            "Coherence": "B1",
        }

    def get_assessment_result(self) -> AssessmentResult:
        """
        Calculate and return final assessment result.

        Must have responses for all questions.

        Returns:
            AssessmentResult with total_score, salary_lpa, and breakdowns

        Raises:
            ValueError: If not all questions have been answered
        """
        # Check all questions answered
        unanswered = [q.id for q in self.questions if q.id not in self.responses]
        if unanswered:
            raise ValueError(
                f"Cannot score: {len(unanswered)} questions unanswered: {unanswered}"
            )

        # Score using ScoringEngine
        question_responses = list(self.responses.values())
        self.assessment_result = ScoringEngine.score_assessment(question_responses)

        return self.assessment_result

    def get_job_recommendations(self) -> Dict:
        """
        Get job recommendations and job radar.

        Must call get_assessment_result() first.

        Returns:
            Dict with:
                - score: final assessment score
                - salary_lpa: calculated salary
                - salary_range: score range info
                - percentile: where candidate sits in spectrum
                - top_10_jobs: recommended jobs

        Raises:
            ValueError: If assessment not yet scored
        """
        if not self.assessment_result:
            raise ValueError("Assessment not yet scored. Call get_assessment_result() first.")

        return RecommendationEngine.get_job_radar(self.assessment_result)

    def get_progress(self) -> Dict:
        """
        Get assessment progress.

        Returns:
            Dict with:
                - total_questions: number of questions
                - answered_questions: number answered
                - pending_questions: number remaining
                - percentage_complete: 0-100
        """
        total = len(self.questions)
        answered = len(self.responses)
        pending = total - answered
        percentage = (answered / total * 100) if total > 0 else 0

        return {
            "total_questions": total,
            "answered_questions": answered,
            "pending_questions": pending,
            "percentage_complete": round(percentage, 1),
        }

    def reset(self):
        """Reset the assessment session."""
        self.session_id = None
        self.responses = {}
        self.assessment_result = None
