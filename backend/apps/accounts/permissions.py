from rest_framework.permissions import BasePermission


class HasPermissionCode(BasePermission):
    """Checks whether the authenticated user has the required permission codes."""

    permission_codes: tuple[str, ...] = ()
    require_all_permissions: bool = True

    def has_permission(self, request, view) -> bool:
        required = None
        getter = getattr(view, "get_required_permissions", None)
        if callable(getter):
            required = getter(request)
        if not required:
            required = getattr(view, "required_permissions", None) or self.permission_codes
        require_all = getattr(view, "require_all_permissions", None)
        if require_all is None:
            require_all = getattr(view, "require_all_permissions", self.require_all_permissions)
        if not required:
            return bool(request.user and request.user.is_authenticated)
        if not request.user or not request.user.is_authenticated:
            return False
        return request.user.has_permission_codes(required, require_all=require_all)

    def has_object_permission(self, request, view, obj) -> bool:
        return self.has_permission(request, view)
