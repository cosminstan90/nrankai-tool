"""
Authentication middleware for Website LLM Analyzer.

Provides optional Basic HTTP Authentication when AUTH_USERNAME and AUTH_PASSWORD
are set in the .env file. If not set, the app runs without authentication.
"""

import os
import secrets
from typing import Optional

from fastapi import Request, HTTPException, status
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware
import base64


def get_auth_credentials() -> Optional[tuple[str, str]]:
    """Get auth credentials from environment. Returns None if not configured."""
    username = os.getenv("AUTH_USERNAME", "").strip()
    password = os.getenv("AUTH_PASSWORD", "").strip()
    if username and password:
        return (username, password)
    return None


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """
    Basic HTTP Authentication middleware.
    
    Skips auth for:
    - Health check endpoint (/api/health)
    - Static files (/static/)
    - If no credentials are configured in .env
    """
    
    SKIP_PATHS = ["/api/health", "/static/", "/favicon.ico", "/contentiq/demo"]
    
    async def dispatch(self, request: Request, call_next):
        credentials = get_auth_credentials()
        
        # If no auth configured, pass through
        if not credentials:
            return await call_next(request)
        
        # Skip auth for certain paths
        path = request.url.path
        if any(path.startswith(skip) for skip in self.SKIP_PATHS):
            return await call_next(request)
        
        # Check Authorization header
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return self._unauthorized_response()
        
        try:
            scheme, encoded = auth_header.split(" ", 1)
            if scheme.lower() != "basic":
                return self._unauthorized_response()
            
            decoded = base64.b64decode(encoded).decode("utf-8")
            username, password = decoded.split(":", 1)
            
            expected_user, expected_pass = credentials
            
            # Constant-time comparison to prevent timing attacks
            user_ok = secrets.compare_digest(username, expected_user)
            pass_ok = secrets.compare_digest(password, expected_pass)
            
            if user_ok and pass_ok:
                return await call_next(request)
            
        except (ValueError, UnicodeDecodeError, base64.binascii.Error):
            pass
        
        return self._unauthorized_response()
    
    def _unauthorized_response(self) -> Response:
        return Response(
            content="Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Website LLM Analyzer"'}
        )
