# Frontend User Management Blueprint

This document outlines how to layer a modern, role-aware frontend on top of the new RBAC backend.

## Core Principles
- **Token-aware shell**: gate all protected routes behind access tokens stored in memory (e.g., React context or Zustand/Recoil store). Persist the refresh token in `httpOnly` cookies if possible; if not, keep it in `localStorage` plus rotating `state.version` to force logout on theft.
- **Optimistic UX**: render based on decoded access-token claims (roles/permissions) while background-refreshing to keep the UI fast and consistent with backend enforcement.
- **Progressive disclosure**: expose dangerous actions (role assignment, deletions) only when the user’s permission list includes the required capability codes.

## Information Architecture
1. **Auth Shell**
   - Public routes: landing, login, registration, forgot-password placeholder.
   - Private routes: everything else. Use a `RequireAuth` wrapper that checks token freshness and optional MFA state.
2. **Dashboard**
   - Surface personalized summary (`/api/accounts/auth/me`), active roles, pending tasks.
3. **User Directory**
   - Search + filter users. Columns: email, name, roles, last login (future), status toggles.
4. **Role Management**
   - Two-panel layout: left list of roles, right detail showing permissions + members.
5. **Audit Feed (later)**
   - Table of recent auth events to aid administrators.

## State & Data Flow
- **AuthContext**
  - Holds `user`, `accessToken`, `refreshToken`, `permissions`, `roles`, `isAuthenticating`.
  - Exposes actions: `login(credentials)`, `register(form)`, `logout()`, `refresh()`.
  - On app boot: read refresh token (if persisted), hit `/api/accounts/auth/refresh/`, repopulate context; fall back to public state if anything fails.
- **React Query (or RTK Query)**
  - Base query attaches the `Authorization: Bearer <accessToken>` header.
  - Interceptor: if a request gets 401 from expired token, automatically call refresh flow (once) before retrying.
- **Permission Hooks**
  - `useCan(requiredPermissions, requireAll = true)` simply checks context permission set.
  - Use for conditional rendering and disabling destructive controls.

## API Integration Cheat Sheet
| Feature | Endpoint | Notes |
|---------|----------|-------|
| Register | `POST /api/accounts/auth/register/` | Body: `{ email, password, first_name, last_name }`. Returns `{ user, access_token, refresh_token, ... }`. |
| Login | `POST /api/accounts/auth/login/` | Same response shape as register. |
| Refresh | `POST /api/accounts/auth/refresh/` | Body: `{ refresh_token }`. |
| Logout | `POST /api/accounts/auth/logout/` | Body: `{ refresh_token }`. |
| Self profile | `GET /api/accounts/auth/me/` | Requires auth. |
| Roles CRUD | `/api/accounts/roles/` | Requires `role.view` / `role.manage`. |
| Membership list | `GET /api/accounts/memberships/?user_id=UUID` | Requires `role.view`. |
| Assign role | `POST /api/accounts/memberships/assign/` | `{ user_id, role_slug, expires_at?, note? }`, requires `role.assign`. |
| Revoke role | `POST /api/accounts/memberships/revoke/` | `{ user_id, role_slug }`, requires `role.assign`. |

## UI Patterns & Components
- **Auth Forms**
  - Use password strength meter and show role assigned (default viewer) post-registration.
  - Display backend errors inline by mapping DRF error keys to form fields.
- **User Table**
  - Data grid with infinite scroll or server-side pagination (query params `page`, `search` once backend supports).
  - Role chips per user (color-coded: viewer = neutral, editor = blue, admin = red).
- **Role Drawer**
  - When selecting a role, open drawer with tabs: *Permissions* (list codes), *Members* (list of users with assign/revoke CTA), *Settings* (priority, requires MFA toggle once exposed).
- **Assignment Modal**
  - Autocomplete for users (typeahead list). Calendar picker for optional expiration.
  - Confirmation step for revoking admin/editor roles.

## Security Considerations
- Always clear auth context + storage on logout.
- Force refresh token rotation by replacing stored refresh token with latest response payload every time `/refresh/` succeeds.
- Handle `mfa_required` flag from tokens to route users to an MFA enrollment screen before unlocking high-privilege areas.
- Audit sensitive mutations (assign/revoke) in the UI by showing toast notifications with timestamps so admins understand state changes.

## Testing Strategy
- Cypress / Playwright e2e flows: register → login → assign/revoke role (admin), viewer denied path.
- Component tests for `RequireAuth` and permission-guarded buttons to ensure they hide/disable when lacking capabilities.
- Contract tests mocking backend responses to ensure token refresh race conditions are handled.

## Implementation Order
1. Auth context + routing guard.
2. Login/registration screens tied to backend endpoints.
3. Global token refresh interceptor.
4. User list & detail views (read-only).
5. Role management UI with assign/revoke modals (guarded by `role.assign`).
6. Polish (empty states, error toasts, skeleton loaders).
