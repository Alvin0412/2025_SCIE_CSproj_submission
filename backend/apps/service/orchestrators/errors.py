from django.http import JsonResponse


class TaskTimeoutError(Exception):
    pass


def handle_async_exception(exc):
    """Convert async task exceptions to HTTP responses."""
    print("Called handle_async_exception with exception:", exc)
    if isinstance(exc, TaskTimeoutError):
        return JsonResponse(
            {"status": "error", "message": f"Task timed out: {str(exc)}"},
            status=504
        )
    # elif isinstance(exc, AsyncTaskInvalidInputError):
    #     return JsonResponse(
    #         {"status": "error", "message": str(exc)},
    #         status=400
    #     )
    # elif isinstance(exc, AsyncTaskFailureError):
    #     return JsonResponse(
    #         {"status": "error", "message": str(exc)},
    #         status=500
    #     )
    return None