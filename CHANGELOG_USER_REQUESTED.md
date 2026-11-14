# BusMate requested UI updates

- Added a shared `/users` route that opens the correct Student or Faculty dashboard based on the authenticated session. Both login success responses now route to `/users`; legacy `/student` and `/faculty` paths remain compatible.
- `/driver` now shows a dedicated driver login page when no driver session exists. The page links to driver registration, and successful registration returns to `/driver` for login.
- Student and Faculty dashboard sections open as a single full-page view. The other dashboard cards are hidden until the user presses **Back to Dashboard**.
- Updated live-map tracking marker to use the supplied tracking-pin image, increased its size, added a clearer blue GPS trail, and made the marker popup show the latest location and coordinates.
- Existing admin login/dashboard and underlying API/data workflows were left in place.

Run with the existing setup: install `requirements.txt`, then start `python app.py`.

## Phase 2 Transport Management Critical Fixes (2026-11-09)

1. **Transport Incharge Complete Removal & Immediate Session Invalidation**:
   - Updated `POST /api/admin/transport-incharge/<id>/remove` to permanently delete the incharge record from SQLite `users` along with foreign-key cleanups (`transport_incharge_buses`, `app_notifications`, `web_push_subscriptions`, `password_reset_otps`).
   - Added `check_session_integrity()` in `@app.before_request` and `transport_incharge_dashboard()` to immediately detect removed/deactivated incharge sessions, clear the session cookies, and block subsequent page/API requests.
   - Removed Incharges permanently disappear from Admin lists and counts.

2. **Blue Button Visibility & High-Contrast Typography Across Website**:
   - Fully audited and fixed `static/modern.css` and `static/style.css` to guarantee solid `#ffffff` white text on all blue buttons (`.btn-primary`, `.primary-btn`, `.btn-top`, `.btn-submit`, `.btn-action`, `button`) with `font-weight: 700 !important;`.
   - Polished high-contrast styling for secondary buttons (white background, `#075fce` visible border, dark text), danger buttons (red `#dc2626`, white text), success buttons (green `#16a34a`, white text), warning buttons (amber `#d97706`, white text), tab buttons, and disabled buttons (`#e2e8f0` muted background, `#64748b` text, `cursor: not-allowed`).

3. **Automatic Unique Bus QR Code Generation on Creation**:
   - Updated `POST /api/admin/bus` (`admin_add_bus`) to accept seating `capacity` (default 50) and automatically generate a unique permanent QR token formatted as `RIT-BUS-<bus_number>-<HEX>`.
   - Transaction safe with automatic database rollback on error.
   - All official college buses have valid permanent QR codes.

4. **Dedicated Bus QR Code Management in Admin Dashboard**:
   - Added top-level Admin navigation card `#cardBusQrManagement` and dedicated section `#busQrManagementSection`.
   - Features real-time search filtering (`#busQrSearchInput`), counter badges, and an interactive fleet table.
   - Integrated "View / Print Poster" modal (`#printBusQrModal`) with professional RIT Transport header, large scannable QR code, and student boarding instructions.
   - Added quick PNG download button and QR regeneration/revocation support.

5. **Student & Faculty Cross-Bus Boarding Support**:
   - Removed the restriction rejecting passengers boarding a different bus (`if user["bus_id"] != bus["id"]`).
   - Students and faculty can now board ANY active college bus by scanning its permanent QR code.
   - Boarding attendance is recorded for the actual scanned bus, correctly incrementing that bus's passenger count while leaving other buses unaffected.
   - Returned rich confirmation feedback with passenger name, actual scanned bus number, route name, boarding time, date, and service type.

6. **Daily Attendance & Bus Capacity Integrity**:
   - Maintained service-type duplicate prevention (`UNIQUE(user_id, travel_date, service_type)`).
   - Enforced seating capacity checks (`if current_boarded >= capacity: 400 Bus Full`).
   - Triggered single broadcast `BUS_FULL` alert to Admin, Driver, and Incharges when maximum capacity is reached.

7. **Transport Incharge Access Control**:
   - Incharges have fleet-wide monitoring access (`/api/incharge/all-bus-status`) without bus assignment restrictions.
   - Blocked detailed passenger attendance reports (`/api/attendance/reports`) with HTTP 403 Forbidden.

1. **Daily Return Bus Setup**:
   - Replaced static default time ("3:50 PM") with an HTML time picker (`<input type="time">`) for each bus.
   - Shows "Not Set" when departure time is unconfigured.
   - Allows admin to set, update, or clear departure times; persisted in SQLite and accurately reflected across student and faculty dashboards.
   - Removed sample/placeholder data from the return bus view.

2. **Manage Buses & Stops Refinement**:
   - Filtered bus stops list to morning boarding points only (`direction = 'MORNING'`).
   - Completely removed evening return timing columns/tables from this view, establishing Daily Return Bus Setup as the single source of truth for return schedules.

3. **Morning Buses UI Modernization**:
   - Redesigned Morning Buses in Student and Faculty dashboards to adopt the exact card structure, badges, typography, spacing, and responsive layout of the Return Buses section (`.route-card`).

4. **Login Pages Redesign**:
   - Modernized Student, Faculty, Admin, Driver, and Transport Incharge login views.
   - Enhanced contrast, focus outlines, distinct role-selector tabs, and password visibility toggles (`👁️`/`🔒`).

5. **Student Registration - Official RIT Programmes**:
   - Restricted department selection to the 12 official RIT programmes (10 UG, 2 PG) from ritchennai.org.
   - Enforced backend validation and stored official names in the database.

6. **Student Class Section**:
   - Added mandatory `class_section` field ('A', 'B', 'C', 'D') for students.
   - Added to registration form, student profile view/edit modal, and admin member rosters.

7. **Automatic Batch Completion & Graduation Lifecycle**:
   - Automated graduation detection: students with expired batch years are marked `lifecycle_status = 'GRADUATED'`, `is_active = 0`.
   - Excluded from active bus manifests and passenger counts, archived in the database, and presented with an informative graduation notice upon login attempts.

8. **Admin Member Deactivation & Reactivation**:
   - Admin can deactivate any passenger account with a mandatory documented reason.
   - Revokes login access and unassigns from bus passenger lists while preserving all records.
   - Admin can view archived members and reactivate them with a single click.

9. **Driver Login Bug Fix**:
   - Unified driver password hashing to PBKDF2 HMAC SHA-256 (500,000 iterations, 16-byte salt).
   - Allows driver login using either Driver Name or Registered Phone Number.
   - Fixed salt mismatch and session initialization issues.

10. **Driver Removal Workflow**:
    - Admin removal deactivates driver account, logs the deactivation reason, detaches active bus assignments, and excludes them from assignment dropdowns.

11. **Admin Dashboard Title**:
    - Updated title banner to "RIT TRANSPORT MANAGEMENT SYSTEM".

12. **Production-Ready Forgot Password OTP**:
    - Configurable SMTP integration via `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER`, `MAIL_USE_SSL`, `MAIL_USE_TLS`.
    - `dev_otp` is strictly suppressed when `FLASK_ENV == 'production'`.

13. **Portal Title Standardization**:
    - Renamed portals to "STUDENT PORTAL" and "FACULTY PORTAL" (removing "Transportation").

14. **Live Bus Tracking Dropdown**:
    - Resolved the "Loading buses..." issue by adding client-side dynamic population from `/api/buses`.

15. **Full Test Suite & Validation**:
    - Phase 1 Integration Tests: 50/50 Passed.
    - Phase 2 Integration Tests: 68/68 Passed.
    - Total: 118/118 Automated Tests Passed (100% pass rate).

## Phase 2 Smart Transport Upgrade (2026-11-09)

1. **Transport Incharge Fleet Monitoring (Section 3 Scope Override)**:
   - Dedicated Transport Incharge portal at `/transport-incharge` with secure login and officer dashboard `/transport-incharge/dashboard`.
   - **No bus assignment**: Incharges monitor all 52 campus buses simultaneously with campus-wide all-bus scope.
   - Comprehensive Fleet Visibility: live tracking coordinates, trip status, delays, seat occupancy (`TOTAL BOARDED TODAY`), scheduled and estimated arrival times, and emergency SOS alerts.
   - Role Boundary Enforcement: strict 403 blocks preventing Transport Incharges from accessing Admin CRUD, passenger personal contact details, student rosters, detailed attendance logs, or private driver phone numbers.
   - Admin CRUD for Transport Incharges in `/admin` (Add, Edit, Activate/Deactivate, Soft Removal).

2. **Digital Boarding Pass & QR Attendance System**:
   - Unique QR code generation for Students and Faculty accessible via `/api/user/qr-token` and interactive "My Bus QR Code" card in passenger dashboards.
   - Prominent text token fallback provided for environments with camera glare or low light.
   - Driver Dashboard QR scanner supporting browser camera (`getUserMedia`) and manual token entry fallback via `POST /api/driver/scan-qr`.
   - Rejection safeguards: duplicate scans on the same service day are rejected with a 409 Conflict; boarding an unassigned bus is rejected with a 400 Bad Request.

3. **Smart Seating Capacity & Bus Full Automation**:
   - Admin-configurable seating capacity (`buses.capacity`, default 50) and scheduled campus arrival time (`buses.scheduled_arrival_time`, default '08:15 AM').
   - Live seat availability endpoint (`GET /api/buses/seat-availability`) clearly displaying `TOTAL BOARDED TODAY` vs Capacity.
   - Automatic `BUS_FULL` alert trigger and global broadcast when boarded count reaches seating capacity.

4. **Dynamic ETA & Automatic Delay Detection**:
   - Real-time Haversine distance and transit duration calculation to RIT Campus (13.0082, 80.0035) via `GET /api/eta/bus/<bus_number>`.
   - Compares estimated arrival with scheduled arrival; automatically classifies delays $\ge 10$ minutes as `RUNNING LATE` and broadcasts delay advisories.

5. **Driver Manual Delay Reporting & Review Workflow**:
   - Instant delay submission via Driver Dashboard modal (`POST /api/driver/report-delay`) capturing reason, expected delay duration, and optional notes.
   - Immediate broadcast to Admin, all active Transport Incharges, and affected bus passengers.
   - Admin & Incharge delay review queue with status management (`REPORTED` $\to$ `UNDER_REVIEW` $\to$ `RESOLVED`).

6. **Driver Emergency SOS & Cancellation Safeguard**:
   - 1-click Emergency SOS activation from Driver Dashboard (`POST /api/driver/emergency-sos`) capturing bus ID, driver ID, GPS fix, emergency classification, and timestamp.
   - Instant campus-wide broadcast to Admin and all active Transport Incharges.
   - Immediate accidental cancellation route (`POST /api/driver/emergency-sos/cancel`) allowing drivers to quickly revoke accidental triggers.
   - Incident acknowledgment and resolution workflow for Admin and Transport Incharges.

7. **In-App Notifications & Web Push Notifications**:
   - Notification bell and drawer in Student, Faculty, and Incharge dashboards.
   - Endpoints for retrieval (`GET /api/notifications`), marking read (`POST /api/notifications/mark-read`), and Web Push subscription (`POST /api/push/subscribe`).

8. **Public Guest Portal at `/newjoiny` & `/guest`**:
   - Fully public, read-only transport routes, morning/evening schedules, and live tracking without registration or login.
   - Strictly hides personal profiles and student/faculty/driver credentials.

## Critical Functional Requirement Update (2026-11-09)

1. **Part 1 – Transport Incharge Login & Account Management**:
   - Incharge login route `/transport-incharge` authenticated using **Transport Incharge Name** and **Password** (case-insensitive name match with fallback to Employee ID, username, or email).
   - Incharge creation in `/admin` includes Full Name, Employee ID, Email, Mobile, Password, and Confirm Password (no bus assignment).
   - Unique name check among Transport Incharges to prevent ambiguous login identities.
   - Secure password hashing using existing architecture with explicit 16-byte random salt (`hash_password(password, salt)`).
   - Inactive or revoked incharge accounts rejected with HTTP 403.

2. **Part 2 – Permanent Bus QR Codes & Student/Faculty Self-Scanning**:
   - Removed passenger scanning from Driver Dashboard; drivers now drive and broadcast live GPS without scanning passengers.
   - Permanent unique QR code generated for each official bus (`buses.qr_code`) formatted as `RIT-BUS-<bus_number>-<HEX>`.
   - Admin Dashboard section **"BUS QR CODE MANAGEMENT"** under Manage Buses with Print Poster, Download QR Image, and Regenerate/Revoke QR capabilities.
   - Printable poster layout compliant with RIT transport specifications (`RIT TRANSPORT`, `Bus Number: R10`, `SCAN TO MARK BUS BOARDING`, high-resolution QR code, student instructions).
   - Added **"Scan Bus QR"** to Student and Faculty dashboards featuring camera scanning via `getUserMedia` (with native `BarcodeDetector` support) and manual code input fallback.
   - `POST /api/passenger/scan-bus-qr` validates authenticated session, active user account, assigned bus match (wrong-bus rejection), service type (`MORNING` vs `RETURN`), duplicate scan prevention, and bus capacity limits.
   - Single `BUS_FULL` alert broadcast to Admin, assigned Driver, and all active Transport Incharges upon reaching seating capacity.
   - Added **"Bus Attendance"** section in `/admin` with date, bus, service type, and passenger search filters, today's count KPI, and CSV export.
   - Transport Incharges restricted from detailed passenger attendance rosters (HTTP 403) while retaining aggregate fleet seat availability monitoring.
