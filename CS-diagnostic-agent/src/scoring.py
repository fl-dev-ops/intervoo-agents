"""
Scoring engine for the diagnostic interview assessment.

Handles:
- Per-dimension scoring for Language (4 independent CEFR dimensions)
- Holistic scoring for Thinking (single TF level)
- Holistic scoring for Confidence (single VCP level)
- Aggregation to final 0-100 score
- Salary band calculation
"""

from typing import List, Dict, Optional
from assessment_models import QuestionResponse, AssessmentResult
from assessment_data import DUMMY_JOBS, SCORE_TO_SALARY_BAND, SALARY_CONFIG


class ScoringEngine:
    """Engine to score assessment responses and generate results."""

    # CEFR level to numeric score mapping (midpoint of range)
    CEFR_SCORE_MAP = {
        "Pre-A1": 12,  # midpoint of 0-24
        "A1": 12,      # midpoint of 0-24
        "A2": 37,      # midpoint of 25-49
        "B1": 37,      # midpoint of 25-49
        "B2": 62,      # midpoint of 50-74
        "C1": 62,      # midpoint of 50-74
        "C2": 87,      # midpoint of 75-100
    }

    # Thinking level to numeric score mapping (midpoint of range)
    THINKING_SCORE_MAP = {
        "TF1": 12,   # midpoint of 0-24
        "TF2": 37,   # midpoint of 25-49
        "TF3": 62,   # midpoint of 50-74
        "TF4": 87,   # midpoint of 75-100
    }

    # Confidence level to numeric score mapping (midpoint of range)
    CONFIDENCE_SCORE_MAP = {
        "VCP1": 12,  # midpoint of 0-24
        "VCP2": 37,  # midpoint of 25-49
        "VCP3": 62,  # midpoint of 50-74
        "VCP4": 87,  # midpoint of 75-100
    }

    @staticmethod
    def score_language_dimension(cefr_level: str) -> float:
        """
        Map a CEFR level to numeric score.

        Args:
            cefr_level: One of Pre-A1, A1, A2, B1, B2, C1, C2

        Returns:
            Numeric score (0-100, using midpoint of range)
        """
        return ScoringEngine.CEFR_SCORE_MAP.get(cefr_level, 0)

    @staticmethod
    def score_thinking(tf_level: str) -> float:
        """
        Map a Thinking level (TF) to numeric score.

        Args:
            tf_level: One of TF1, TF2, TF3, TF4

        Returns:
            Numeric score (0-100, using midpoint of range)
        """
        return ScoringEngine.THINKING_SCORE_MAP.get(tf_level, 0)

    @staticmethod
    def score_confidence(vcp_level: str) -> float:
        """
        Map a Confidence level (VCP) to numeric score.

        Args:
            vcp_level: One of VCP1, VCP2, VCP3, VCP4

        Returns:
            Numeric score (0-100, using midpoint of range)
        """
        return ScoringEngine.CONFIDENCE_SCORE_MAP.get(vcp_level, 0)

    @staticmethod
    def score_language(language_levels: Dict[str, str]) -> float:
        """
        Score Language by averaging 4 independent CEFR dimensions.

        Each dimension (Fluency, Accuracy, Range, Coherence) is scored
        independently at different CEFR levels and then averaged.

        Args:
            language_levels: Dict mapping dimension -> CEFR level
                            e.g., {"Fluency": "A2", "Accuracy": "B1", ...}

        Returns:
            Average Language score (0-100)
        """
        if not language_levels:
            return 0

        scores = [
            ScoringEngine.score_language_dimension(level)
            for level in language_levels.values()
        ]
        return sum(scores) / len(scores) if scores else 0

    @staticmethod
    def score_question(response: QuestionResponse) -> float:
        """
        Score a single question by averaging Thinking, Confidence, Language.

        Args:
            response: QuestionResponse with TF, VCP, and Language levels

        Returns:
            Question score (0-100)
        """
        tf_score = ScoringEngine.score_thinking(response.thinking_level)
        vcp_score = ScoringEngine.score_confidence(response.confidence_level)
        lang_score = ScoringEngine.score_language(response.language_levels)

        return (tf_score + vcp_score + lang_score) / 3

    @staticmethod
    def score_assessment(question_responses: List[QuestionResponse]) -> AssessmentResult:
        """
        Generate final assessment result by averaging all question scores.

        Args:
            question_responses: List of QuestionResponse objects

        Returns:
            AssessmentResult with final score and recommendations
        """
        if not question_responses:
            raise ValueError("No question responses provided")

        # Score each question
        question_scores = [
            ScoringEngine.score_question(response)
            for response in question_responses
        ]

        # Calculate dimension-specific averages
        thinking_scores = [
            ScoringEngine.score_thinking(resp.thinking_level)
            for resp in question_responses
        ]
        confidence_scores = [
            ScoringEngine.score_confidence(resp.confidence_level)
            for resp in question_responses
        ]
        language_scores = [
            ScoringEngine.score_language(resp.language_levels)
            for resp in question_responses
        ]

        # Final aggregated scores
        final_score = sum(question_scores) / len(question_scores)
        thinking_avg = sum(thinking_scores) / len(thinking_scores)
        confidence_avg = sum(confidence_scores) / len(confidence_scores)
        language_avg = sum(language_scores) / len(language_scores)

        # Calculate salary using config from assessment_data
        min_salary = SALARY_CONFIG["min_lpa"]
        max_salary = SALARY_CONFIG["max_lpa"]
        salary_lpa = min_salary + (final_score / 100) * (max_salary - min_salary)
        salary_lpa = round(salary_lpa, 2)

        # Determine salary band and percentile
        salary_band = SALARY_CONFIG["salary_band_label"]
        salary_percentile = final_score / 100  # 0.75 for score 75

        return AssessmentResult(
            total_score=round(final_score, 2),
            thinking_avg=round(thinking_avg, 2),
            confidence_avg=round(confidence_avg, 2),
            language_avg=round(language_avg, 2),
            salary_lpa=salary_lpa,
            salary_band=salary_band,
            salary_percentile=round(salary_percentile, 3),
            question_responses=question_responses,
        )


class RecommendationEngine:
    """Engine to generate job recommendations based on score."""

    @staticmethod
    def get_score_range(score: float) -> str:
        """
        Determine which score range a score falls into.

        Args:
            score: Assessment score (0-100)

        Returns:
            Score range string (e.g., "50-74")
        """
        if score < 25:
            return "0-24"
        elif score < 50:
            return "25-49"
        elif score < 75:
            return "50-74"
        else:
            return "75-100"

    @staticmethod
    def get_recommendations(score: float) -> Dict:
        """
        Get job recommendations and salary band for a given score.

        Args:
            score: Assessment score (0-100)

        Returns:
            Dict with:
                - score_range: e.g., "50-74"
                - salary_lpa: e.g., 25.0
                - salary_band: "10-40 LPA"
                - percentile: e.g., 0.50
                - recommended_jobs: List of top 10 jobs
        """
        score_range = RecommendationEngine.get_score_range(score)

        # Calculate salary using config from assessment_data
        min_salary = SALARY_CONFIG["min_lpa"]
        max_salary = SALARY_CONFIG["max_lpa"]
        salary_lpa = min_salary + (score / 100) * (max_salary - min_salary)
        salary_lpa = round(salary_lpa, 2)

        percentile = score / 100

        return {
            "score_range": score_range,
            "salary_lpa": salary_lpa,
            "salary_band": SALARY_CONFIG["salary_band_label"],
            "percentile": round(percentile, 3),
            "recommended_jobs": DUMMY_JOBS.get(score_range, []),
        }

    @staticmethod
    def get_job_radar(assessment_result: AssessmentResult) -> Dict:
        """
        Generate job radar display info.

        Shows where the candidate sits in the salary spectrum and
        lists top 10 recommended jobs.

        Args:
            assessment_result: AssessmentResult object

        Returns:
            Dict with job radar info
        """
        recommendations = RecommendationEngine.get_recommendations(
            assessment_result.total_score
        )

        return {
            "score": assessment_result.total_score,
            "salary_lpa": assessment_result.salary_lpa,
            "salary_range": f"{recommendations['score_range']} → {SALARY_CONFIG['salary_band_label']}",
            "percentile": f"{int(assessment_result.salary_percentile * 100)}th percentile",
            "top_10_jobs": recommendations["recommended_jobs"],
            "message": f"Based on your score of {assessment_result.total_score}, you can target roles paying {assessment_result.salary_lpa} LPA in the {recommendations['score_range']} range.",
        }
