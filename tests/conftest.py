"""
tests/conftest.py

Registers Hypothesis profiles so they are available across all test modules.

Usage:
    pytest --hypothesis-profile=prism tests/unit/
"""

from hypothesis import HealthCheck, settings

# "prism" profile: 100 examples, suppress too_slow health check (needed for
# tests that involve sentence-transformer inference or other heavy setup).
settings.register_profile(
    "prism",
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow],
)

# "ci" profile: fewer examples for fast CI feedback
settings.register_profile(
    "ci",
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
)

# "dev" profile: minimal examples for rapid local iteration
settings.register_profile(
    "dev",
    max_examples=10,
    suppress_health_check=[HealthCheck.too_slow],
)

# Default to the "prism" profile if no --hypothesis-profile flag is passed
settings.load_profile("prism")
