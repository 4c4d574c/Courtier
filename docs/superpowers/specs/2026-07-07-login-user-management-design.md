# Login Page & User Management — Design Spec

**Date:** 2026-07-07
**Status:** Approved (pending implementation plan)

## Overview

Add multi-user login/registration and user management to SDTAgent. Currently the system has only a single hardcoded admin user. This design introduces a two-role user system (admin + auditor), a login page with split-panel layout, self-service registration with admin approval, and a table-style user management admin panel.

## Design Decisions Summary

| Decision | Choice |
|----------|--------|
| User model | Multi-user, two roles (admin, auditor) |
| Account creation | Open registration + admin approval |
| User management features | Full: list, approve/disable, reset password, profile editing |
| Login page visual | Split layout — left brand area + right form |
| User management layout | Table-style list with search, filter, pagination |
| Session persistence | httpOnly refresh-token cookie + JWT access token in memory |
| Registration page | Complete standalone page (same split-layout template) |
| Navigation | Independent admin panel at `/admin/*` with sidebar |
| Roles | Two: admin (full control) + auditor (audit + view own sessions) |

---

## Frontend

### Router

Introduce Vue Router. Route table:

| Route | Page | Auth Required |
|-------|------|---------------|
| `/login` | Login page — split layout (left brand + right form) | No |
| `/register` | Registration page — same split layout, right side = registration form | No |
| `/` | Audit main interface — existing Terminal component | Yes |
| `/admin/users` | User management — table list | Admin only |
| `/admin/approvals` | Registration approvals — pending list | Admin only |
| `/profile` | Personal settings — change password, etc. | Yes |

### Navigation

- Top bar right side: user avatar/name dropdown menu
  - **Auditor:** Profile settings → Logout
  - **Admin (extra):** User management, Registration approvals
- Admin links navigate to independent `/admin/*` pages with dedicated sidebar navigation

### Route Guards

- `beforeEach`: unauthenticated → redirect to `/login`
- Admin routes: check role, non-admin → redirect to `/`

### Login Page (`/login`)

Split-panel layout consistent with existing "Editorial Warm / Paper" design system:
- **Left panel:** Brand display — seal icon, "SDTAgent" name, "文档智能审计平台" tagline, paper-grain texture background
- **Right panel:** Login form — username, password, submit button (seal-red), link to registration page
- Error states: invalid credentials inline message, rate-limit feedback

### Registration Page (`/register`)

Same split-panel template as login:
- **Left panel:** Same brand display
- **Right panel:** Registration form — username, email, password, confirm password, submit
- Post-submit: redirect to login with "等待管理员审批" (awaiting admin approval) message

### User Management (`/admin/users`)

Table-style layout:
- Search input + role/status filter tabs
- Columns: username, email, role badge, status badge, registration date, actions (···)
- Row actions: edit role, enable/disable, reset password
- Pagination

### Registration Approvals (`/admin/approvals`)

- Table of pending users
- Approve / Reject buttons per row
- Empty state when no pending approvals

### Profile (`/profile`)

- Change password form (current password + new password + confirm)
- Edit email

### Components to Create

```
webui/src/
├── router/
│   └── index.ts              # Vue Router setup + guards
├── views/
│   ├── LoginView.vue         # Login page
│   ├── RegisterView.vue      # Registration page
│   ├── AdminLayout.vue       # Admin shell (sidebar + content)
│   ├── UserManagement.vue    # Admin user table
│   ├── ApprovalManagement.vue # Admin approvals table
│   └── ProfileView.vue       # User profile/settings
├── composables/
│   └── useAuth.ts            # Auth state, login/logout/refresh, token management
├── api/
│   └── client.ts             # Updated: cookie-based refresh, auto-refresh on 401
└── styles/
    └── auth.css              # Login/register split-layout styles
```

### Auth Composable (`useAuth`)

- `user` — reactive current user (null when not logged in)
- `isAdmin` — computed boolean
- `login(username, password)` — call POST /api/auth/login, store access token in memory
- `register(username, email, password)` — call POST /api/auth/register
- `logout()` — call POST /api/auth/logout, clear state
- `refreshToken()` — call POST /api/auth/refresh, update access token
- `initAuth()` — on app mount, attempt token refresh to restore session
- Auto-refresh interceptor: on 401 response, attempt refresh, retry original request once

---

## Backend

### User Model

Table: `users`

| Column | Type | Constraints |
|--------|------|-------------|
| id | INT | PK, AUTO_INCREMENT |
| username | VARCHAR(64) | UNIQUE, NOT NULL |
| password_hash | VARCHAR(256) | NOT NULL (bcrypt) |
| email | VARCHAR(128) | — |
| role | ENUM('admin','auditor') | NOT NULL, DEFAULT 'auditor' |
| status | ENUM('active','disabled','pending') | NOT NULL, DEFAULT 'pending' |
| avatar_url | VARCHAR(256) | — |
| created_at | DATETIME | — |
| updated_at | DATETIME | — |

### RefreshToken Model

Table: `refresh_tokens`

| Column | Type | Constraints |
|--------|------|-------------|
| id | INT | PK, AUTO_INCREMENT |
| user_id | INT | FK → users.id |
| token_hash | VARCHAR(256) | SHA-256 of random token |
| expires_at | DATETIME | — |
| revoked | BOOLEAN | DEFAULT FALSE |

### API Endpoints

#### Auth (public)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/auth/register` | Register. Creates user with status=pending. Rate limited: 3/hour per IP |
| POST | `/api/auth/login` | Login. Validates credentials, sets refresh_token httpOnly cookie, returns access token in body |
| POST | `/api/auth/refresh` | Reads refresh_token from cookie, validates against DB, returns new access token + rotates refresh token |
| POST | `/api/auth/logout` | Revokes refresh token, clears cookie |

#### User Management (admin only)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/users` | List users. Query params: `page`, `page_size`, `search`, `role`, `status` |
| GET | `/api/admin/users/:id` | User detail |
| PATCH | `/api/admin/users/:id` | Update user: role, status. Body: `{role?, status?, password?}` |
| GET | `/api/admin/approvals` | List pending-approval users |
| POST | `/api/admin/approvals/:id/approve` | Approve registration → status=active |
| POST | `/api/admin/approvals/:id/reject` | Reject registration → delete user record |

#### Profile (authenticated)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/profile` | Get current user profile |
| PATCH | `/api/profile` | Update own profile: `{email?, current_password?, new_password?}` |

### Token Strategy

- **Access Token:** JWT (HS256), 15-minute expiry, stored in JS memory variable, sent via `Authorization: Bearer` header
- **Refresh Token:** Random 64-byte hex string, SHA-256 hashed before DB storage, 7-day expiry, delivered via `Set-Cookie` as httpOnly + Secure + SameSite=Strict cookie with path `/api/auth`
- **Refresh flow:** Frontend 401 interceptor → calls `/api/auth/refresh` → on success, retries original request; on failure, redirects to `/login`
- **Logout:** Revokes refresh token in DB, clears cookie via `Set-Cookie` with max-age=0

### Password Policy

- Minimum 8 characters
- bcrypt hashing (cost factor 12)
- Reset password: admin sets new password via PATCH `/api/admin/users/:id`; user changes own via PATCH `/api/profile` (requires current password)

### Role Permissions

| Action | Admin | Auditor |
|--------|-------|---------|
| Create audit tasks | ✅ | ✅ |
| View own sessions | ✅ | ✅ |
| Manage users | ✅ | ❌ |
| Approve registrations | ✅ | ❌ |
| System configuration | ✅ | ❌ |

### Migration Notes

- Existing `admin_user` / `admin_password` env vars: used to create initial admin user on first migration (Alembic data migration). After migration, admin login uses the DB table; env vars become bootstrap-only.
- Existing `documents.user_id` column: remains a string column; populated with user ID from JWT.
- Session ownership (`SessionRecord.owner`): already implemented; continues to work with JWT `sub` claim.

### Files to Create/Modify

```
docaudit-agent/
├── src/dbop/tables/
│   ├── user.py              # NEW: User ORM model
│   └── refresh_token.py     # NEW: RefreshToken ORM model
├── src/agent/api/
│   ├── routes/
│   │   ├── auth.py          # MODIFY: extend with register, refresh, logout
│   │   ├── admin_users.py   # NEW: admin user management routes
│   │   └── profile.py       # NEW: profile routes
│   ├── middleware/
│   │   └── auth.py          # MODIFY: add refresh token creation/validation
│   └── app.py               # MODIFY: register new routers
├── alembic/versions/        # NEW: migration for users + refresh_tokens tables
└── .env.example             # MODIFY: document new user-related settings
```

---

## Visual Design

All pages follow the existing "Editorial Warm / Paper" design system:

- **Colors:** seal-red (`#b71c2e`), gold (`#8b6914`), ink (`#2d2520`), paper (`#faf7f2`)
- **Typography:** Noto Serif SC (headings), Noto Sans SC (body), JetBrains Mono (code)
- **CSS:** Pure CSS with custom properties from `tokens.css`, no UI framework

### Login / Register (Shared Layout)

- Left: 50% width, warm paper gradient background with subtle grain texture, centered brand elements (seal icon, app name, tagline)
- Right: 50% width, white/paper background, centered form card (max 360px), form fields with paper-colored backgrounds and subtle borders, seal-red submit button
- Registration adds: email field, confirm password field, link back to login

### Admin Panel

- Sidebar (240px): seal icon (small) + navigation items (用户管理, 注册审批), active item with seal-red left border accent
- Content area: white card with table, filter bar above, pagination below
- Status badges: green (active), amber (pending), red (disabled)
- Role badges: gold outline (admin), neutral outline (auditor)

---

## Cross-Cutting Concerns

### Security

- [x] Passwords hashed with bcrypt (cost 12)
- [x] Refresh tokens stored as SHA-256 hashes (not plaintext)
- [x] httpOnly + Secure + SameSite=Strict cookies
- [x] Rate limiting: login 5/min, register 3/hour
- [x] Timing-safe password comparison
- [x] All admin routes require admin role check (not just authentication)

### Testing

- Backend: unit tests for auth middleware (token create/verify/refresh/revoke), integration tests for all new API endpoints
- Frontend: tests for `useAuth` composable, route guard behavior, login/register form validation

### Observability

- Auth events (login, logout, register, approve, reject) logged via existing OpenTelemetry instrumentation
- Prometheus counter: `auth_login_total`, `auth_register_total`, `auth_refresh_total`

---

## Out of Scope

- OAuth / SSO integration
- MFA / 2FA
- Email verification (registration uses admin approval instead)
- Password reset via email (admin resets manually)
- User avatar upload (URL only for now)
- Session timeout / idle detection
- Account lockout after repeated failed logins (rate limiter provides basic protection)
