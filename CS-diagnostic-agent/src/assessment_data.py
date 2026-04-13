"""
Dummy data for the diagnostic interview assessment.

Contains questions, rubrics, and recommendation mappings.
Ready to be swapped when Cathy & DJ provide real data.
"""

from assessment_models import Question, Rubric, RubricLevel


# ============================================================================
# THINKING RUBRIC (TF 1-4)
# ============================================================================

THINKING_RUBRIC = Rubric(
    framework="TF 1-4",
    dimension_name="Thinking",
    scoring_note="Holistic across 4 dimensions. No single dimension creates or breaks the level. Pattern across all four defines it.",
    sub_dimensions=["Relevance", "Specificity", "Reasoning", "Job Competency"],
    levels={
        "TF1": RubricLevel(
            level_name="TF1",
            label="Not shown",
            score_range=(0, 24),
            descriptors={
                "Relevance": "Does not address the question. Talks about the topic broadly. Response could be an answer to any question. No connection drawn between the question and their answer.",
                "Specificity": "No concrete detail anywhere in the response. No situation named, no numbers, no task described, no tools or people mentioned. Response is hollow and could not be verified or followed up on.",
                "Reasoning": "No reasoning visible. States what happened or what they would do with no explanation of why or how. Pure description or assertion. No thinking process surfaced at any point.",
                "Job Competency": "Cannot demonstrate the competency at all. Knows the word for the behaviour but offers no story, no action, no consequence. Interviewer has nothing to evaluate.",
            },
        ),
        "TF2": RubricLevel(
            level_name="TF2",
            label="Partial",
            score_range=(25, 49),
            descriptors={
                "Relevance": "Understands roughly what was asked but addresses it only partially. Covers one aspect of the question and misses another. Some connection between question and response but not complete alignment.",
                "Specificity": "Some detail present but thin. Mentions a situation or context but without enough information to make it credible. One concrete element surrounded by vague generalities.",
                "Reasoning": "Reasoning is attempted but shallow. Explains what was done, not why. A reason is given but not examined. No recognition of alternatives, constraints, or trade-offs.",
                "Job Competency": "Competency is described but not fully demonstrated. A story starts but is incomplete. Action is present but outcome or impact is absent. Interviewer can see the competency might be there but cannot confirm it.",
            },
        ),
        "TF3": RubricLevel(
            level_name="TF3",
            label="Developing",
            score_range=(50, 74),
            descriptors={
                "Relevance": "Directly addresses the question. Clear and consistent connection between what was asked and what was answered. Stays on topic throughout. Does not drift or miss the point.",
                "Specificity": "Concrete and credible. Situation is named and contextualised. Details are specific enough to be real. Numbers, steps, or names present where appropriate. Response could be followed up without breaking down.",
                "Reasoning": "Reasoning is visible and structured. Explains why a decision was made. Acknowledges at least one alternative or constraint. Cause and effect are connected, not just described.",
                "Job Competency": "Behaviour is demonstrated, not just claimed. Response has situation, action, and outcome. The competency is shown through what happened. Interviewer can form a judgment from what is described.",
            },
        ),
        "TF4": RubricLevel(
            level_name="TF4",
            label="Strong",
            score_range=(75, 100),
            descriptors={
                "Relevance": "Addresses the question and anticipates what a strong answer requires. Shows understanding of the underlying concern behind the question, not just the literal words. Goes where a thoughtful interviewer would want the answer to go.",
                "Specificity": "Rich, verifiable detail throughout. Multiple concrete elements — specific situation, named tools or people, quantified outcomes. No vague language anywhere. Response would survive close questioning on every detail.",
                "Reasoning": "Sophisticated, deliberate reasoning. Weighs options explicitly, identifies trade-offs, shows awareness of what was at stake. Reasoning leads to a confident conclusion that is explained and owned by the candidate.",
                "Job Competency": "Competency demonstrated with clear impact. Outcome is specific and attributable to the candidate's actions. Interviewer walks away with a strong, evidence-based impression of what this person can do in the role.",
            },
        ),
    },
)


# ============================================================================
# CONFIDENCE RUBRIC (VCP 1-4)
# ============================================================================

CONFIDENCE_RUBRIC = Rubric(
    framework="VCP 1-4",
    dimension_name="Confidence",
    scoring_note="Holistic listener impression across 4 audio signals. Not a checklist — signals inform the level, they do not each produce a sub-score. Score by asking: what does the overall pattern of these four signals create in a listener's experience?",
    sub_dimensions=["Volume", "Pace", "Pause", "Latency"],
    levels={
        "VCP1": RubricLevel(
            level_name="VCP1",
            label="Absent",
            score_range=(0, 24),
            descriptors={
                "Volume": "Consistently below audible threshold. Listener must strain or lean in to hear. Words are regularly lost. Volume may drop to near-silence on any question that requires thinking. Mumbling is the dominant pattern throughout.",
                "Pace": "No controlled rhythm at any point. Either words blur together and sentences run into each other so the listener cannot follow, or long dead spaces open between words suggesting the candidate has completely lost their thread. Pace does not recover between questions.",
                "Pause": "Either mid-sentence silences of 3 seconds or more — the candidate has frozen — or no pauses at all, with speech delivered in one anxious unbroken rush. Both patterns signal a complete loss of control. The listener cannot predict when a thought will end.",
                "Latency": "Either very long silences before speaking — consistently over 6 seconds — suggesting the candidate is overwhelmed. Or near-zero latency — under 1 second — suggesting a panic response where the candidate begins speaking before they have any idea what they will say.",
            },
        ),
        "VCP2": RubricLevel(
            level_name="VCP2",
            label="Inconsistent",
            score_range=(25, 49),
            descriptors={
                "Volume": "Audible on safe questions — resume, personal topics, anything rehearsed. Drops noticeably by roughly 20% or more when the question becomes harder. Sentences regularly trail off before the final word, meaning key information is lost precisely when it matters most.",
                "Pace": "Reasonable and followable on familiar content. Disrupts significantly on harder questions — either speeds up noticeably so words start running together, or stalls into slow halting delivery with long gaps between phrases. Does not return to baseline pace within the same answer.",
                "Pause": "Some natural pausing between sentences on easy questions. Pauses become long and frequent on any question that requires thinking. Filled pauses — um, uh, like — may appear frequently, breaking the flow and drawing attention away from content.",
                "Latency": "Normal 2-4 second latency on opening questions. Latency increases significantly on every harder question. Before behavioural and situational questions it may stretch to 6 seconds or more consistently. Pattern tells the listener that confidence is entirely topic-dependent.",
            },
        ),
        "VCP3": RubricLevel(
            level_name="VCP3",
            label="Emerging",
            score_range=(50, 74),
            descriptors={
                "Volume": "Consistently audible across most of the session. Clear enough that the listener never has to strain. Maintains good projection on behavioural questions even when the content is personal or uncomfortable. May drop slightly on the hardest question — the drop is noticeable but not severe — and voice returns to baseline before the next question begins.",
                "Pace": "Controlled and consistent for most of the session. The listener can follow every sentence comfortably. A natural slight slowing on complex questions signals the candidate is thinking, not freezing. Some acceleration when cognitive demand rises — candidate notices and adjusts. Pace is back to baseline before the answer ends.",
                "Pause": "Purposeful pausing throughout. Brief beats before answering complex questions — 1 to 3 seconds — that feel considered rather than lost. Pauses between sentences are natural and well-timed. Occasional longer pause mid-sentence on the hardest questions — may stretch to 2-3 seconds once or twice — but candidate recovers quickly and the answer proceeds clearly.",
                "Latency": "Generally within 2-4 seconds across the session. Slightly longer on situational questions — 4 to 5 seconds — which is appropriate and reads as genuine consideration. One or two questions may prompt a longer latency but this does not become a pattern. When it occurs, candidate begins speaking and delivers a coherent answer.",
            },
        ),
        "VCP4": RubricLevel(
            level_name="VCP4",
            label="Confident",
            score_range=(75, 100),
            descriptors={
                "Volume": "Clear and well-projected throughout the session. The listener receives every word without effort. Volume is consistent across familiar and unfamiliar questions — it does not collapse when the content gets hard. A very slight natural drop on the closing question is normal and human. It does not persist.",
                "Pace": "Controlled and steady across the whole session — broadly within the 120 to 150 WPM conversational range. A slight natural slowing on complex questions is present and actually positive — it signals deliberate thinking. The pace belongs to the candidate. It does not change based on the difficulty of the question.",
                "Pause": "Pausing is purposeful and well-placed throughout. The candidate uses silence deliberately — a beat before a considered answer, a natural breath between sentences. Short pauses of 1 to 2 seconds before hard questions feel confident, not lost. The listener reads every pause as intentional.",
                "Latency": "Normal 2-4 second latency across the entire session. Does not spike significantly on harder questions. The candidate begins speaking at a consistent timing regardless of question type — they have a process for handling any question and the latency reflects it.",
            },
        ),
    },
)


# ============================================================================
# LANGUAGE RUBRIC (CEFR Pre-A1 to B2)
# ============================================================================

LANGUAGE_RUBRIC = Rubric(
    framework="CEFR",
    dimension_name="Language",
    scoring_note="Score each dimension independently. A student can be B1 Fluency and A2 Accuracy simultaneously. Do NOT flatten scores.",
    sub_dimensions=["Fluency", "Accuracy", "Range", "Coherence"],
    levels={
        "Pre-A1": RubricLevel(
            level_name="Pre-A1",
            label="Pre-beginner",
            score_range=(0, 24),
            descriptors={
                "Fluency": "Cannot produce connected speech. Only isolated words or memorised phrases.",
                "Accuracy": "No grammatical control. Cannot form sentences.",
                "Range": "Survival vocabulary only. Fewer than 100 words.",
                "Coherence": "Cannot link ideas. Only isolated words.",
            },
        ),
        "A1": RubricLevel(
            level_name="A1",
            label="Breakthrough",
            score_range=(0, 24),
            descriptors={
                "Fluency": "Very slow with long pauses (3+ seconds), many false starts, frequent fillers (3-4 per sentence). Speech frequently breaks down.",
                "Accuracy": "Limited control of basic structures. Missing verbs and severe tense mixing. Frequent errors that affect clarity.",
                "Range": "Basic high-frequency vocabulary only. Very repetitive — thing, stuff, good, bad, do.",
                "Coherence": "Minimal linking. Can link words or phrases with simple connectors — and, then.",
            },
        ),
        "A2": RubricLevel(
            level_name="A2",
            label="Waystage",
            score_range=(25, 49),
            descriptors={
                "Fluency": "Hesitations frequently interrupt idea completion. Short idea units of 1-2 sentences. Pauses followed by minimal recovery. Communication is fragmented.",
                "Accuracy": "Basic structures generally correct but systematic errors remain. Meaning understandable but sentence structure unstable. Errors in verb forms and sentence construction.",
                "Range": "Limited but adequate for routine needs. Familiar everyday words. Heavy repetition of same lexical items with little variation.",
                "Coherence": "Basic linear sequencing with simple connectors — and, but, because, so. Ideas loosely connected. May drift from question.",
            },
        ),
        "B1": RubricLevel(
            level_name="B1",
            label="Threshold",
            score_range=(25, 49),
            descriptors={
                "Fluency": "Speaks in extended stretches of 3+ sentences on one topic. Can resume and elaborate after pauses. Provides examples and detailed explanations. Pauses show thinking with clear recovery and continued communication.",
                "Accuracy": "Good control of simple structures. Errors mainly in complex forms. Meaning always clear.",
                "Range": "Sufficient range for familiar professional contexts. Varied vocabulary. Professional and abstract terms emerge — achieve, require, coordinate, implement, concepts. Technical vocabulary relevant to field.",
                "Coherence": "Clear logical structure with varied connectors — first, then, finally, however, although. Stays on topic. Ideas logically organised and supported with examples.",
            },
        ),
        "B2": RubricLevel(
            level_name="B2",
            label="Vantage",
            score_range=(50, 74),
            descriptors={
                "Fluency": "Natural, flexible flow throughout. Seamless transitions and recovery. Extends ideas effortlessly.",
                "Accuracy": "Consistent control with few errors. Self-corrects spontaneously.",
                "Range": "Broad vocabulary range with flexibility. Precise, nuanced word choice.",
                "Coherence": "Well-structured discourse with sophisticated linking devices.",
            },
        ),
    },
)


# ============================================================================
# DUMMY QUESTIONS
# ============================================================================

DUMMY_QUESTIONS = [
    # Opening questions
    Question(
        id="q001",
        text="Tell me about yourself and your background.",
        question_type="Language",
        category="opening",
        difficulty_level="easy",
    ),
    Question(
        id="q002",
        text="What are you most proud of in your career so far?",
        question_type="Thinking",
        category="opening",
        difficulty_level="easy",
    ),
    # Domain questions
    Question(
        id="q003",
        text="Explain the concept of object-oriented programming and give an example.",
        question_type="Thinking",
        category="domain",
        difficulty_level="medium",
    ),
    Question(
        id="q004",
        text="Walk me through how you would optimize a slow SQL query.",
        question_type="Thinking",
        category="domain",
        difficulty_level="hard",
    ),
    Question(
        id="q005",
        text="Describe a complex technical problem you solved recently.",
        question_type="Language",
        category="domain",
        difficulty_level="hard",
    ),
    # Behavioral questions
    Question(
        id="q006",
        text="Tell me about a time you had to work with a difficult team member.",
        question_type="Thinking",
        category="behavioral",
        difficulty_level="medium",
    ),
    Question(
        id="q007",
        text="Describe a situation where you had to learn something quickly.",
        question_type="Confidence",
        category="behavioral",
        difficulty_level="medium",
    ),
    Question(
        id="q008",
        text="How do you handle failure or setbacks?",
        question_type="Thinking",
        category="behavioral",
        difficulty_level="medium",
    ),
    # Closing questions
    Question(
        id="q009",
        text="Why are you interested in this role?",
        question_type="Language",
        category="closing",
        difficulty_level="easy",
    ),
    Question(
        id="q010",
        text="What are your career goals for the next 5 years?",
        question_type="Thinking",
        category="closing",
        difficulty_level="medium",
    ),
]


# ============================================================================
# SALARY & JOB MAPPING (Placeholder)
# ============================================================================

# Salary configuration (will be refined when Cathy & DJ finalize)
SALARY_CONFIG = {
    "min_lpa": 10,
    "max_lpa": 40,
    "salary_band_label": "10-40 LPA",
}

# Score range to salary band (will be refined when Cathy & DJ finalize)
SCORE_TO_SALARY_BAND = {
    "0-24": {"lpa_min": 10, "lpa_max": 15, "label": "Entry-level"},
    "25-49": {"lpa_min": 15, "lpa_max": 20, "label": "Mid-level"},
    "50-74": {"lpa_min": 20, "lpa_max": 30, "label": "Senior"},
    "75-100": {"lpa_min": 30, "lpa_max": 40, "label": "Lead/Specialist"},
}

# Dummy job recommendations by score range
DUMMY_JOBS = {
    "0-24": [
        "Junior Developer",
        "Support Engineer",
        "QA Tester",
        "Data Analyst (Entry)",
        "Technical Writer",
        "Frontend Developer (Junior)",
        "Backend Developer (Junior)",
        "Systems Administrator",
        "Network Technician",
        "Database Administrator (Junior)",
    ],
    "25-49": [
        "Software Developer",
        "Senior Support Engineer",
        "QA Engineer",
        "Data Analyst",
        "Solutions Architect (Junior)",
        "Full Stack Developer",
        "DevOps Engineer",
        "Cloud Engineer",
        "Machine Learning Engineer (Junior)",
        "Security Engineer",
    ],
    "50-74": [
        "Senior Software Engineer",
        "Technical Lead",
        "Solutions Architect",
        "Senior Data Scientist",
        "Engineering Manager",
        "Principal Engineer",
        "Platform Engineer",
        "Security Architect",
        "Database Architect",
        "Staff Engineer",
    ],
    "75-100": [
        "Principal Engineer",
        "Director of Engineering",
        "VP Engineering",
        "Chief Technology Officer",
        "Distinguished Engineer",
        "Chief Architect",
        "VP Product Engineering",
        "Head of Research",
        "Engineering Lead (Specialized)",
        "Technical Fellow",
    ],
}
