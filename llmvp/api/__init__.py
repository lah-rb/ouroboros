"""
API module initialization.

Exports FastAPI application for GraphQL API.
The REST API is available as an optional OpenAI compatibility shim
when app.openai_shim: true is set in the configuration.
"""

from .graphql_api import app as graphql_app

__all__ = ["graphql_app"]
