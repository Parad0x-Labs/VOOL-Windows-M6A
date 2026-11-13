"""A question that names a filesystem path is the file and folder lanes' material, never adjudicated.

Captest, real Windows hardware: "what files are in C:\\Users\\Satoshi\\Documents\\vool-test?" passed
`single_plain_know_question`, so the entity-ambiguity probe ran; when the probe failed the user was told
"I couldn't complete my check for whether this question has a single well-known answer" and the folder
was never listed. A turn naming a FILE was already out of the probe's jurisdiction; a turn naming a
PATH (drive, share, home-rooted or POSIX-rooted) is out of it for the same reason.
"""

from __future__ import annotations

import pytest

from core.entity_ambiguity import single_plain_know_question
from core.input_normalizer import normalize_user_text

PATH_QUESTIONS = [
    r"what files are in C:\Users\Satoshi\Documents\vool-test?",
    r"list the files in C:\Users\Satoshi\Documents\vool-test",
    "what files are in C:/Users/Satoshi/Documents/vool-test?",
    r"what files are in \\nas\share\vool-test?",
    r"list the files in \\nas\share\vool-test",
    "what files are in ~/vool-test?",
    "list the files in ~/Documents/vool-test",
    "what files are in /home/x/vool-test?",
    "what files are in /Users/satoshi/vool-test?",
    "list the files in /tmp/vool-test",
]


@pytest.mark.parametrize("question", PATH_QUESTIONS)
def test_a_question_naming_a_path_is_not_adjudicated(question):
    assert single_plain_know_question(question) is False


# The probe is handed the normalized turn. A UNC share does not survive the normalizer as a share
# (`\\nas\share` reaches the runtime as `\ \ nas \ share`), so only the forms that do are checked here.
@pytest.mark.parametrize("question", [q for q in PATH_QUESTIONS if not q.split(" in ", 1)[1].startswith("\\\\")])
def test_the_normalized_path_question_is_not_adjudicated_either(question):
    assert single_plain_know_question(normalize_user_text(question).normalized_text) is False


@pytest.mark.parametrize(
    "question",
    [
        "who is the mayor of Springfield?",
        "What is the population of Springfield?",
        "What is the capital of France?",
    ],
)
def test_a_plain_knowledge_question_keeps_its_eligibility(question):
    assert single_plain_know_question(question) is True


# Slashes that are not a path: a URL's authority and path, a fraction, a ratio, a word pair.
@pytest.mark.parametrize(
    "question",
    [
        "what does http://localhost/ point to?",
        "what is the history of the URL scheme in http://intranet/wiki?",
        "Is 1/2 a rational number?",
        "what does the term 24/7 mean?",
        "who coined the phrase and/or?",
    ],
)
def test_a_slash_that_is_not_a_path_keeps_its_eligibility(question):
    assert single_plain_know_question(question) is True
