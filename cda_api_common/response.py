from typing import Any, Optional


def success_response(data: Any = None, message: str = "success"):
    return {
        "success": True,
        "message": message,
        "data": data,
    }


def error_response(message: str = "Internal server error"):
    return {
        "message": message,
        "data": None,
    }
