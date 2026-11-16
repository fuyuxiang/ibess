"""Resolve the administrator attached by the authentication middleware."""

from fastapi import HTTPException


def actor(request):
    admin = getattr(request.state, "admin", None)
    if not admin or not admin.get("username"):
        raise HTTPException(401, "请先登录")
    return admin["username"]
