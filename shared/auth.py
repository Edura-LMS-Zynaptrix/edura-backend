# Shared authentication utilities
from functools import wraps

def require_role(roles: list[str]):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Placeholder for JWT verification and role checking
            # In a real implementation, this would decode the JWT token and check claims
            return await func(*args, **kwargs)
        return wrapper
    return decorator
