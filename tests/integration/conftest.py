# tests/integration/conftest.py — Integration test fixtures
#
# Provides shared fixtures for full pipeline integration tests:
#   - FastAPI TestClient with mocked AI/DB dependencies
#   - Sample log fixtures for common CI/CD platforms
#   - Mock responses for AI API calls

from __future__ import annotations

import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


# ============================================================
#  Sample log fixtures
# ============================================================

@pytest.fixture
def github_actions_log():
    """Realistic GitHub Actions npm build failure log."""
    return """2024-01-15T14:30:00.1234567Z ##[group]Run npm ci
2024-01-15T14:30:01.2345678Z npm ERR! code ERESOLVE
2024-01-15T14:30:02.3456789Z npm ERR! ERESOLVE could not resolve
2024-01-15T14:30:03.4567890Z npm ERR! While resolving: react@18.2.0
2024-01-15T14:30:04.5678901Z npm ERR! Found: @testing-library/react@13.4.0
2024-01-15T14:30:05.6789012Z npm ERR! Could not resolve dependency:
2024-01-15T14:30:06.7890123Z npm ERR! peer react@"^17.0.0" from @testing-library/react@13.4.0
2024-01-15T14:30:07.8901234Z npm ERR! Fix the upstream dependency conflict, or retry
2024-01-15T14:30:08.9012345Z npm ERR! this command with --legacy-peer-deps
2024-01-15T14:30:09.0123456Z ##[error]Process completed with exit code 1.
"""


@pytest.fixture
def docker_build_log():
    """Realistic Docker build failure log."""
    return """#0 building with "default" instance using docker driver
#1 [internal] load build definition from Dockerfile
#1 transferring dockerfile: 542B done
#1 DONE 0.0s
#2 [internal] load .dockerignore
#2 transferring context: 2B done
#2 DONE 0.0s
#3 [internal] load metadata for docker.io/library/python:3.11-slim
#3 DONE 1.2s
#4 [1/5] FROM docker.io/library/python:3.11-slim@sha256:abc123
#4 DONE 0.0s
#5 [2/5] WORKDIR /app
#5 DONE 0.1s
#6 [3/5] COPY requirements.txt .
#6 DONE 0.0s
#7 [4/5] RUN pip install -r requirements.txt
#7 0.345 Collecting numpy==1.99.0
#7 0.567   ERROR: Could not find a version that satisfies the requirement numpy==1.99.0
#7 0.789   ERROR: No matching distribution found for numpy==1.99.0
#7 1.234 ERROR: failed to solve: process "/bin/sh -c pip install -r requirements.txt" did not complete successfully: exit code: 1
------
 > [4/5] RUN pip install -r requirements.txt:
0.345 Collecting numpy==1.99.0
0.567   ERROR: Could not find a version that satisfies the requirement numpy==1.99.0
0.789   ERROR: No matching distribution found for numpy==1.99.0
------
Dockerfile:15
--------------------
  13 |     COPY requirements.txt .
  14 |
  15 | >>> RUN pip install -r requirements.txt
  16 |
  17 |     COPY . .
--------------------
ERROR: failed to solve: process "/bin/sh -c pip install -r requirements.txt" did not complete successfully: exit code: 1
"""


@pytest.fixture
def jenkins_log():
    """Jenkins pipeline failure log."""
    return """[Pipeline] Start of Pipeline
[Pipeline] node
Running on Jenkins in /var/jenkins_home/workspace/build-job
[Pipeline] stage
[Pipeline] { (Build)
[Pipeline] sh
+ ./gradlew build
FAILURE: Build failed with an exception.
* Where:
Build file '/var/jenkins_home/workspace/build-job/build.gradle' line: 42
* What went wrong:
Could not resolve all files for configuration ':compileClasspath'.
> Could not find com.example:missing-lib:1.0.0
* Try:
Run with --stacktrace option to get the stack trace.
[Pipeline] }
ERROR: Build failed
[Pipeline] End of Pipeline
ERROR: script returned exit code 1
Finished: FAILURE
"""


@pytest.fixture
def large_log():
    """Oversized log (> 100KB) to trigger resource guard."""
    base = "2024-01-15 14:30:00 ERROR Something went wrong in module xyz\n"
    # Repeat to exceed 100KB
    return base * 2500


@pytest.fixture
def normal_log():
    """Normal sized log for standard testing."""
    return """2024-01-15 14:30:00 INFO Build started
2024-01-15 14:30:05 ERROR ImportError: No module named 'requests'
2024-01-15 14:30:10 ERROR Build failed with exit code 1
"""


@pytest.fixture
def mock_ai_response_dict():
    """Standard mock AI analysis response."""
    return {
        "error_summary": "npm dependency resolution conflict",
        "error_detail": "npm ERR! ERESOLVE could not resolve dependency tree",
        "root_causes": [
            {"description": "react version incompatibility with testing-library", "probability": 90},
            {"description": "Outdated package-lock.json", "probability": 10},
        ],
        "fix_suggestions": [
            {
                "title": "Use --legacy-peer-deps flag",
                "description": "Bypass peer dependency resolution",
                "command": "npm install --legacy-peer-deps",
                "safety_level": "safe",
            },
            {
                "title": "Update testing-library for React 18",
                "description": "Use compatible version with React 18",
                "command": "npm install @testing-library/react@latest",
                "safety_level": "safe",
            },
        ],
        "debug_commands": ["npm ls react", "npm why testing-library"],
        "severity": "medium",
        "prevention": ["Use more flexible version ranges in package.json"],
        "security_warning": "",
    }


# ============================================================
#  FastAPI TestClient fixture
# ============================================================

@pytest.fixture
def test_client():
    """Create a FastAPI TestClient with mocked dependencies.

    Mocks the OpenAI client and Qdrant to avoid real API calls.
    """
    # Mock OpenAI client at module level before importing api
    mock_openai = MagicMock()
    mock_qdrant = MagicMock()

    with patch("openai.OpenAI", return_value=mock_openai), \
         patch("qdrant_client.QdrantClient", return_value=mock_qdrant), \
         patch("sentence_transformers.SentenceTransformer", return_value=MagicMock()):
        # Must import after mocking
        from api.main import app
        client = TestClient(app)
        yield client


@pytest.fixture
def mock_ai_call():
    """Mock the AI engine call to return a controlled response."""
    with patch("ai_engine.call_ai_structured") as mock_call:
        from models import AnalysisResult, RootCause, FixSuggestion
        result = AnalysisResult(
            error_summary="Mocked analysis result",
            error_detail="Mocked detail for testing",
            root_causes=[RootCause(description="Mocked root cause", probability=85)],
            fix_suggestions=[
                FixSuggestion(
                    title="Mocked fix",
                    description="Run this command",
                    command="echo 'fix applied'",
                    safety_level="safe",
                )
            ],
            debug_commands=["echo debug"],
            severity="high",
            prevention=["Mock prevention tip"],
            security_warning="",
        )
        mock_call.return_value = result
        yield mock_call
