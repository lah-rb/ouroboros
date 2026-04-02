"""
Mock test to verify testing infrastructure works
"""


def test_mock():
    """A simple mock test that should always pass"""
    assert True


if __name__ == "__main__":
    # This allows running the test directly
    import pytest

    pytest.main([__file__, "-v"])
