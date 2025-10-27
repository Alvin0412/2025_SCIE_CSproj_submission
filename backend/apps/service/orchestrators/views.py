import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from backend.apps.service.orchestrators.registry import resolve_future, get_registry_stats, get_pending_futures, start_cleanup, stop_cleanup


@csrf_exempt
async def resolve_task(request):
    """处理任务结果回调"""
    body = json.loads(request.body.decode("utf-8"))
    print(f"Received task resolution request: {body}")
    task_id = body["task_id"]
    error = body.get("error")
    result = body.get("result")
    if error:
        resolve_future(task_id, Exception(error), is_error=True)
    else:
        resolve_future(task_id, result)
    return JsonResponse({"ok": True})


@require_http_methods(["GET"])
def registry_stats(request):
    """获取注册表统计信息"""
    stats = get_registry_stats()
    return JsonResponse({
        "status": "success",
        "data": stats
    })


@require_http_methods(["GET"])
def pending_futures(request):
    """获取当前待处理的 Future 信息（调试用）"""
    pending = get_pending_futures()
    return JsonResponse({
        "status": "success",
        "data": {
            "count": len(pending),
            "futures": pending
        }
    })


@require_http_methods(["POST"])
def cleanup_registry(request):
    """手动触发清理操作"""
    try:
        # 这里可以添加手动清理逻辑
        return JsonResponse({
            "status": "success",
            "message": "Cleanup triggered"
        })
    except Exception as e:
        return JsonResponse({
            "status": "error",
            "message": str(e)
        }, status=500)


@require_http_methods(["POST"])
def start_cleanup_service(request):
    """启动清理服务"""
    try:
        start_cleanup()
        return JsonResponse({
            "status": "success",
            "message": "Cleanup service started"
        })
    except Exception as e:
        return JsonResponse({
            "status": "error",
            "message": str(e)
        }, status=500)


@require_http_methods(["POST"])
def stop_cleanup_service(request):
    """停止清理服务"""
    try:
        stop_cleanup()
        return JsonResponse({
            "status": "success",
            "message": "Cleanup service stopped"
        })
    except Exception as e:
        return JsonResponse({
            "status": "error",
            "message": str(e)
        }, status=500)
