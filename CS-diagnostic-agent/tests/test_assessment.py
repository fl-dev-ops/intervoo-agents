"""
Unit tests for the diagnostic interview assessment backend.

Tests cover:
- Complete assessment flow
- Scoring logic with different response levels
- Salary calculations
- Error handling
- Progress tracking
"""

import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from assessment_service import AssessmentService
from assessment_models import QuestionResponse, AssessmentResult
from scoring import ScoringEngine, RecommendationEngine
from assessment_data import DUMMY_QUESTIONS


class TestAssessmentFlow:
    """Tests for complete assessment flow."""

    def test_start_assessment(self):
        """Test starting a new assessment."""
        service = AssessmentService()
        questions = service.start_assessment()

        # Should return all dummy questions
        assert len(questions) == 10
        assert all(hasattr(q, "id") for q in questions)
        assert all(hasattr(q, "text") for q in questions)
        # Session should be initialized
        assert service.session_id is not None

    def test_submit_response(self):
        """Test submitting a response to a question."""
        service = AssessmentService()
        questions = service.start_assessment()
        first_question = questions[0]

        response = service.submit_response(
            question_id=first_question.id,
            raw_response="This is a sample answer to the question.",
        )

        # Response should have all levels assigned
        assert response.question_id == first_question.id
        assert response.thinking_level in ["TF1", "TF2", "TF3", "TF4"]
        assert response.confidence_level in ["VCP1", "VCP2", "VCP3", "VCP4"]
        assert "Fluency" in response.language_levels
        assert "Accuracy" in response.language_levels
        assert "Range" in response.language_levels
        assert "Coherence" in response.language_levels

    def test_complete_assessment_flow(self):
        """Test complete flow: start → submit all → score → recommend."""
        service = AssessmentService()
        questions = service.start_assessment()

        # Submit response for each question
        for question in questions:
            service.submit_response(
                question_id=question.id,
                raw_response="Sample response to the question.",
            )

        # Get assessment result
        result = service.get_assessment_result()

        # Verify result structure
        assert isinstance(result, AssessmentResult)
        assert 0 <= result.total_score <= 100
        assert 10 <= result.salary_lpa <= 40
        assert 0 <= result.thinking_avg <= 100
        assert 0 <= result.confidence_avg <= 100
        assert 0 <= result.language_avg <= 100

        # Get recommendations
        recommendations = service.get_job_recommendations()
        assert "score" in recommendations
        assert "salary_lpa" in recommendations
        assert "top_10_jobs" in recommendations
        assert len(recommendations["top_10_jobs"]) == 10

    def test_submit_response_invalid_question_id(self):
        """Test that submitting response to non-existent question raises error."""
        service = AssessmentService()
        service.start_assessment()

        with pytest.raises(ValueError):
            service.submit_response(
                question_id="invalid_id",
                raw_response="Sample response",
            )

    def test_assessment_result_before_all_answered(self):
        """Test that getting result before all questions answered raises error."""
        service = AssessmentService()
        questions = service.start_assessment()

        # Only answer first question
        service.submit_response(
            question_id=questions[0].id,
            raw_response="Sample response",
        )

        # Should raise error - not all questions answered
        with pytest.raises(ValueError):
            service.get_assessment_result()

    def test_job_recommendations_before_scoring(self):
        """Test that getting recommendations before scoring raises error."""
        service = AssessmentService()
        service.start_assessment()

        with pytest.raises(ValueError):
            service.get_job_recommendations()


class TestScoring:
    """Tests for scoring logic."""

    def test_thinking_score_mapping(self):
        """Test Thinking level to score mapping."""
        assert ScoringEngine.score_thinking("TF1") == 12
        assert ScoringEngine.score_thinking("TF2") == 37
        assert ScoringEngine.score_thinking("TF3") == 62
        assert ScoringEngine.score_thinking("TF4") == 87

    def test_confidence_score_mapping(self):
        """Test Confidence level to score mapping."""
        assert ScoringEngine.score_confidence("VCP1") == 12
        assert ScoringEngine.score_confidence("VCP2") == 37
        assert ScoringEngine.score_confidence("VCP3") == 62
        assert ScoringEngine.score_confidence("VCP4") == 87

    def test_language_dimension_score_mapping(self):
        """Test CEFR level to score mapping."""
        assert ScoringEngine.score_language_dimension("Pre-A1") == 12
        assert ScoringEngine.score_language_dimension("A1") == 12
        assert ScoringEngine.score_language_dimension("A2") == 37
        assert ScoringEngine.score_language_dimension("B1") == 37
        assert ScoringEngine.score_language_dimension("B2") == 62
        assert ScoringEngine.score_language_dimension("C1") == 62
        assert ScoringEngine.score_language_dimension("C2") == 87

    def test_language_score_averages_dimensions(self):
        """Test that Language score averages 4 dimensions independently."""
        language_levels = {
            "Fluency": "A2",  # 37
            "Accuracy": "B1",  # 37
            "Range": "A2",     # 37
            "Coherence": "B1",  # 37
        }
        # Average of [37, 37, 37, 37] = 37
        assert ScoringEngine.score_language(language_levels) == 37

    def test_language_score_mixed_dimensions(self):
        """Test Language score with mixed CEFR levels."""
        language_levels = {
            "Fluency": "B2",   # 62
            "Accuracy": "A2",  # 37
            "Range": "B1",     # 37
            "Coherence": "B1",  # 37
        }
        # Average of [62, 37, 37, 37] = 43.25
        expected = (62 + 37 + 37 + 37) / 4
        assert ScoringEngine.score_language(language_levels) == expected

    def test_question_score_averages_dimensions(self):
        """Test that question score averages TF, VCP, and Language."""
        response = QuestionResponse(
            question_id="test_q",
            thinking_level="TF3",  # 62
            confidence_level="VCP3",  # 62
            language_levels={
                "Fluency": "B1",
                "Accuracy": "B1",
                "Range": "B1",
                "Coherence": "B1",  # All 37
            },
        )
        # Average of [62, 62, 37] = 53.67
        expected = (62 + 62 + 37) / 3
        assert abs(ScoringEngine.score_question(response) - expected) < 0.1

    def test_all_low_scores(self):
        """Test assessment with all TF1/VCP1/Pre-A1 responses."""
        service = AssessmentService()
        questions = service.start_assessment()

        # Mock by directly creating low-score responses
        for question in questions:
            service.responses[question.id] = QuestionResponse(
                question_id=question.id,
                thinking_level="TF1",  # 12
                confidence_level="VCP1",  # 12
                language_levels={
                    "Fluency": "Pre-A1",
                    "Accuracy": "Pre-A1",
                    "Range": "Pre-A1",
                    "Coherence": "Pre-A1",
                },  # All 12
            )

        result = service.get_assessment_result()

        # Average of [12, 12, 12] = 12 for each question, final = 12
        assert result.total_score == 12
        assert result.salary_lpa == 13.6  # 10 + (12/100) * 30

    def test_all_high_scores(self):
        """Test assessment with all TF4/VCP4/C2 responses."""
        service = AssessmentService()
        questions = service.start_assessment()

        # Mock by directly creating high-score responses
        for question in questions:
            service.responses[question.id] = QuestionResponse(
                question_id=question.id,
                thinking_level="TF4",  # 87
                confidence_level="VCP4",  # 87
                language_levels={
                    "Fluency": "C2",
                    "Accuracy": "C2",
                    "Range": "C2",
                    "Coherence": "C2",
                },  # All 87
            )

        result = service.get_assessment_result()

        # Average of [87, 87, 87] = 87 for each question, final = 87
        assert result.total_score == 87
        assert result.salary_lpa == 36.1  # 10 + (87/100) * 30

    def test_mixed_score_calculation(self):
        """Test assessment with mixed response levels."""
        service = AssessmentService()
        questions = service.start_assessment()

        # First 5 questions: TF3
        for i in range(5):
            service.responses[questions[i].id] = QuestionResponse(
                question_id=questions[i].id,
                thinking_level="TF3",  # 62
                confidence_level="VCP3",  # 62
                language_levels={
                    "Fluency": "B1",
                    "Accuracy": "B1",
                    "Range": "B1",
                    "Coherence": "B1",
                },  # All 37
            )

        # Last 5 questions: TF1
        for i in range(5, 10):
            service.responses[questions[i].id] = QuestionResponse(
                question_id=questions[i].id,
                thinking_level="TF1",  # 12
                confidence_level="VCP1",  # 12
                language_levels={
                    "Fluency": "Pre-A1",
                    "Accuracy": "Pre-A1",
                    "Range": "Pre-A1",
                    "Coherence": "Pre-A1",
                },  # All 12
            )

        result = service.get_assessment_result()

        # Each TF3 question: (62+62+37)/3 = 53.67
        # Each TF1 question: (12+12+12)/3 = 12
        # Average: (53.67*5 + 12*5) / 10 = 32.835
        expected = ((62 + 62 + 37) / 3 * 5 + (12 + 12 + 12) / 3 * 5) / 10
        assert abs(result.total_score - expected) < 1  # Allow small rounding


class TestSalaryCalculation:
    """Tests for salary mapping."""

    def test_salary_score_zero(self):
        """Test salary calculation for score 0."""
        # 10 + (0/100) * 30 = 10
        assert RecommendationEngine.get_recommendations(0)["salary_lpa"] == 10.0

    def test_salary_score_fifty(self):
        """Test salary calculation for score 50."""
        # 10 + (50/100) * 30 = 25
        assert RecommendationEngine.get_recommendations(50)["salary_lpa"] == 25.0

    def test_salary_score_hundred(self):
        """Test salary calculation for score 100."""
        # 10 + (100/100) * 30 = 40
        assert RecommendationEngine.get_recommendations(100)["salary_lpa"] == 40.0

    def test_salary_score_seventy_five(self):
        """Test salary calculation for score 75 (middle of high range)."""
        # 10 + (75/100) * 30 = 32.5
        assert RecommendationEngine.get_recommendations(75)["salary_lpa"] == 32.5


class TestRecommendations:
    """Tests for job recommendations."""

    def test_score_range_low(self):
        """Test score range classification for low scores."""
        assert RecommendationEngine.get_score_range(12) == "0-24"
        assert RecommendationEngine.get_score_range(0) == "0-24"
        assert RecommendationEngine.get_score_range(24) == "0-24"

    def test_score_range_medium_low(self):
        """Test score range classification for medium-low scores."""
        assert RecommendationEngine.get_score_range(37) == "25-49"
        assert RecommendationEngine.get_score_range(25) == "25-49"
        assert RecommendationEngine.get_score_range(49) == "25-49"

    def test_score_range_medium_high(self):
        """Test score range classification for medium-high scores."""
        assert RecommendationEngine.get_score_range(62) == "50-74"
        assert RecommendationEngine.get_score_range(50) == "50-74"
        assert RecommendationEngine.get_score_range(74) == "50-74"

    def test_score_range_high(self):
        """Test score range classification for high scores."""
        assert RecommendationEngine.get_score_range(87) == "75-100"
        assert RecommendationEngine.get_score_range(75) == "75-100"
        assert RecommendationEngine.get_score_range(100) == "75-100"

    def test_recommendations_have_jobs(self):
        """Test that recommendations include job list."""
        for score in [12, 37, 62, 87]:
            recommendations = RecommendationEngine.get_recommendations(score)
            assert "recommended_jobs" in recommendations
            assert len(recommendations["recommended_jobs"]) == 10


class TestProgress:
    """Tests for progress tracking."""

    def test_progress_initial(self):
        """Test progress at start."""
        service = AssessmentService()
        service.start_assessment()
        progress = service.get_progress()

        assert progress["total_questions"] == 10
        assert progress["answered_questions"] == 0
        assert progress["pending_questions"] == 10
        assert progress["percentage_complete"] == 0.0

    def test_progress_partial(self):
        """Test progress after answering some questions."""
        service = AssessmentService()
        questions = service.start_assessment()

        # Answer 3 out of 10
        for i in range(3):
            service.submit_response(
                question_id=questions[i].id,
                raw_response="Sample response",
            )

        progress = service.get_progress()
        assert progress["answered_questions"] == 3
        assert progress["pending_questions"] == 7
        assert progress["percentage_complete"] == 30.0

    def test_progress_complete(self):
        """Test progress when all questions answered."""
        service = AssessmentService()
        questions = service.start_assessment()

        # Answer all questions
        for question in questions:
            service.submit_response(
                question_id=question.id,
                raw_response="Sample response",
            )

        progress = service.get_progress()
        assert progress["answered_questions"] == 10
        assert progress["pending_questions"] == 0
        assert progress["percentage_complete"] == 100.0


class TestReset:
    """Tests for session reset."""

    def test_reset_clears_state(self):
        """Test that reset clears session state."""
        service = AssessmentService()
        questions = service.start_assessment()

        # Submit some responses
        service.submit_response(
            question_id=questions[0].id,
            raw_response="Sample response",
        )

        # Reset
        service.reset()

        assert service.session_id is None
        assert len(service.responses) == 0
        assert service.assessment_result is None
