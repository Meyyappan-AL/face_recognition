# PROJECT.md

Last updated: 2026-11-27

This file is the source of truth for continuing the ERP project in a new chat. Read this first, then run the verification commands near the bottom before making changes.

## 1. Project Overview

- **Project:** Flask academic ERP with manual period-wise attendance, OD workflow, timetables, electives, staff/HOD/advisor management, notification center, audit log, reports, and student dashboard.
- **Working directory:** `C:\Users\alagu\OneDrive\Documents\Desktop\git repo\face_recognition`
- **Main files:** `app.py`, `model.py`, `templates/`, `static/`, `timetables/`
- **Database:** SQLite `attendance.db` (local runtime file, ignored by git; never commit)
- **UI stack:** Bootstrap 5.3.1, vanilla JavaScript, custom `static/css/erp.css` and `static/js/erp.js`
- **Python:** Must be below Python 3.13 for the optional MediaPipe path. Current install scripts enforce this.
- **Platform used for development:** Windows PowerShell 5.1. `rg` is unavailable, so use the built-in `grep` tool instead of shell `rg`.

## 2. Hard Constraints and Scope

- Keep the project flat and simple: `app.py`, `model.py`, `templates/`, `static/`, `timetables/`.
- Do not commit runtime data or local secrets:
  - `attendance.db`, `*.db`
  - `dataset/`
  - `uploads/`, `uploads/od_proofs/`, and `uploads/face_attendance/`
  - `model.pkl`, `*.pkl`
  - `train_status.json`, `*.pyc`
  - `.env`
- Do not commit or print real SMTP/Fast2SMS/VAPID secrets.
- Current scope is **local-only**: manual period attendance plus admin-only webcam/uploaded-video attendance. Cloud hosting, Supabase/Postgres, Cloudflare R2, and CCTV/RTSP are deferred until staff approval. Manual attendance remains the primary path.
- Current server behavior persists stored `-` unmarked rows from class date `06:00` until explicit marking. Automatic `Absent` rows were migrated to `-` in Wave 2.
- Student identity and duplicate prevention are keyed on `reg_no`, **not** roll number.
- Admin/HOD can override attendance restrictions after cutoff until the semester end date; future attendance is never editable and all manual changes close after semester end. Advisors have full-day access only for their own class.
- No real notification credentials are configured in `.env`; notification sending must remain optional and gracefully disabled.

## 3. Current High-Level Status

The ERP is functional for the main admin, staff, HOD/advisor, and student flows. Recent work added notification infrastructure, active-account enforcement, dependency/env files, a shared base template/nav for most admin pages, persisted default-Absent materialization, semester-end edit windows, day-of OD application, semester-scoped timetable versions with a manual grid editor and PDF/image reference upload, version-scoped elective mappings with immutable per-entry elective labels and a shared elective roster, role-scoped attendance insights with comparisons/absence filters, medical leave and student analytics, and admin-only webcam/video face attendance with review-before-submit. Wave 2 adds real `N` status, stored `-` unmarked periods, configurable marking window, in-app staff unmarked-period reminders, and calendar/timetable/elective polish. Wave 3 adds webcam lifecycle/permission reliability fixes and optional YuNet/SFace model support with graceful fallback. Wave 4 completes records/reports finalization with filter-aware exports, staff-scoped CSVs, custom date ranges, clearer `Absent/N` wording, IST-safe defaults, and print/layout polish.

Batch-semester redesign (Phases 1-5) is implemented: batches 2023-27/2024-28/2025-29/2026-30, S1-S8 per batch with configurable default date boundaries, batch-relative auto-current semester, admin rollover preview/apply with audit and current-state-only renames, timetable/elective batch+sem selectors, S5-S8 elective gating, started-sem visibility for staff/students and all 1-8 for admin, and batch-sem propagation to reports/records/student dashboard/admin student attendance.

### Completed and verified

- Batch-semester redesign: `semesters` rows carry `batch_start_year,sem_no,is_auto` (unique per batch+sem); `class_cohorts` tracks stable cohort identity/current class; S1-S8 auto-seeded for 2023-2026 batches; default odd `01/08-31/12`, even `01/01-20/04`; current sem auto-calc by batch/date with admin boundary edits/reset.
- Rollover: admin preview/apply by batch (S1->S2 ... S7->S8), audited, current-state only (`students.class/study_year`, staff advisor class, cohort current class, timetable JSON mirror); historical attendance/medical/OD/notifications/audit class names are not rewritten.
- Timetable/electives: admin timetable/electives use batch+Sem 1-8 selectors; timetable version/import/apply/restore accept batch+sem context; electives enabled only S5-S8 and blocked/hidden S1-S4; staff/students cannot edit current/previous-sem timetable/elective data, admin can; attendance edit-window rules unchanged.
- Batch-sem propagation: reports/analytics, attendance records, admin student attendance, student dashboard APIs/JS, elective export/import resolve `batch_start_year+sem_no`; staff/students see started sems only, admin sees all 1-8.
- Timetable-backed classes are dynamic via `get_classes()`. Current timetable classes are `24IT5A` and `24IT5B`.
- Staff sync from timetables preserves existing passwords by default.
- Admin pages are available for:
  - Students
  - Staff/HOD/advisor management
  - Academic calendar
  - Timetable versions + manual grid editor
  - Elective mapping
  - Notification center
  - ERP settings
  - Audit log
  - Records and reports
  - Webcam/video face attendance
- Manual attendance supports class, date, period 1-7, statuses Present/Absent/N/OD/Late/Medical Leave, plus `-` to reset a period to unmarked.
- OD workflow supports submission, advisor/HOD review, proof, expiry, cancellation/reversal, and student history.
- Medical leave workflow supports mandatory certificates, advisor then HOD review, automatic attendance crediting, HOD cancellation/reversal, student cancellation, review/history views, and contact-only advisor coordination (no online notifications).
- Student dashboard analytics now include a threshold warning, goal calculator, monthly heatmap, subject/elective breakdown, today/next class, and CSV export; `Medical Leave` is credited like Present/Late/OD.
- Student dashboard is rebuilt and now loads its assets correctly.
- Reports and defaulter exports exist.
- Reports and records now support role-scoped insights, class/staff/period/student comparisons, absence filters, status donut charts, and absent-period/hour metrics.
- Admin face attendance runs locally: one-student-at-a-time webcam capture, 550 MB classroom video upload with background processing, student/unknown deduplication, low-confidence flags, manual unknown-face assignment, and review-before-submit. Uploaded videos are deleted after review.
- Phase 7 scale verification passed with a synthetic 75-student roster: 60 recognized students were collapsed into 60 rows, unknown groups were deduplicated/capped, and a 75-row face-attendance submit completed successfully.
- Redesign Phase 1 added student batch/study-year fields, filterable/sortable admin student list, a read-only per-student attendance page, and removed the remaining legacy attendance UI sections.
- Wave 1 added optional student roles with `Rep` badges, expanded staff management with extended profiles/archive status, and redesigned settings/audit pages.
- Role-aware navigation: students see Dashboard / My Academics / OD / Medical / Alerts / Profile / Logout; staff/HOD see their operational pages plus Medical Review for HOD/advisor; admin additionally sees Semesters and Face Attendance.
- Admin-managed semesters (`semesters` table) with one current semester; legacy settings are seeded/synced for compatibility.
- Student dashboard and staff reports/records accept a semester dropdown. Current semester runs start..today; completed semester runs start..last scheduled working day.
- Student dashboard stats now use the full scheduled semester grid: unmarked past periods show `-` and are excluded from totals, future periods remain Not Marked, `N` counts as absent, and percentages use explicitly marked periods only.
- Unmarked `-` rows are persisted by a background worker with idempotent materialization; edit windows enforce class-date 06:00 to day-15 22:00, admin/HOD corrections until semester end, and reject future writes.
- Approved OD now applies date-by-date at/after 06:00 IST, defers future dates, and preserves explicit Present/Late.
- Student dashboard overview UI cleaned: semester selector, concise range text, calendar moved into a collapsed panel, and marking-log modal shows real class/slot/source values.
- Notification infrastructure exists:
  - `student_alerts`
  - `notification_outbox`
  - `push_subscriptions`
  - Email/SMS/Web Push sender functions
  - Per-channel and per-event settings
  - Admin settings/test/process/retry UI
  - Background worker starts at app import
  - DB-level outbox claim (`claimed_at` + attempts compare-and-swap) with stale-claim recovery for multi-worker safety
  - Admin retry now targets the requested outbox row (`process_notification_outbox(only_ids=[...])`)
- Student Web Push support was added:
  - `static/sw.js`
  - Root-scoped `/sw.js` route with `Service-Worker-Allowed: /`
  - Push subscription UI and read/read-all alert actions
- Inactive-account enforcement was added:
  - Login rejects inactive users/students.
  - `login_required` and `role_required` invalidate sessions for deactivated accounts.
  - API requests return 401 JSON; page requests redirect to `/login`.
- Elective choices now override the displayed subject in the student timetable/dashboard and in staff attendance/report views for mapped slots.
- Elective mappings are version-scoped: `student_electives.timetable_version_id` selects mappings per timetable version, with nullable legacy rows kept as fallback.
- Attendance entries snapshot `elective_label` on manual submit, default-Absent materialization, and OD application; roster/report/dashboard views prefer the stored label so historical records do not change when mappings are edited.
- The admin elective page supports semester/version selection, mapped/missing/unassigned/conflict counts, legacy-source badges, CSV import/export per version, carry-forward from the prior version, and recent elective audit visibility.
- Both elective staff can mark the full shared class roster; assignment checks also recognize staff codes listed inside elective option labels.
- Hardcoded class dropdowns were replaced with dynamic classes in the main templates. Remaining `24IT5A`/`24IT5B` occurrences are example text only.
- Literal `` `r`n `` artifacts were removed from the pages where they were found.
- **Base template migration mostly complete:** every main admin/staff/student page now extends `base.html`; `index.html`, `staff_monitor.html`, `reports.html`, `attendance_record.html`, `take_attendance.html`, `od.html`, `profile.html`, and `student_dashboard.html` were migrated and smoke-tested. The only remaining standalone template is `login.html` (custom auth shell).
- `requirements.txt` and `.env.example` were added.
- `install.sh` and `run.sh` were updated to use `requirements.txt` and cleaner output.

### Base template migration status

New shared files exist:

- `templates/base.html`
- `templates/partials/erp_nav.html`

`base.html` provides:
- Bootstrap and ERP CSS links
- Bootstrap JS and `erp.js` at the bottom
- Blocks: `title`, `head`, `body_class`, `shell_class`, `nav`, `content`, `modals`, `scripts`

`partials/erp_nav.html` provides role-aware navigation:
- Admin: Dashboard, Students, Staff, Calendar, Timetable, Electives, Notifications, Settings, Audit, Records, Reports, Staff View, Profile, Logout
- Staff/HOD: Dashboard, Take Attendance, Records, OD Review, Reports, Medical Review (HOD/advisor), Profile, Logout
- Student: Dashboard, My Academics, OD, Medical, Alerts, Profile, Logout

**Migrated to `base.html` (all server-rendered 200):**

- `templates/index.html` (Chart.js in `head`, dashboard JS in `scripts`)
- `templates/staff_monitor.html`
- `templates/reports.html`
- `templates/attendance_record.html`
- `templates/take_attendance.html`
- `templates/od.html`
- `templates/medical.html`
- `templates/profile.html`
- `templates/student_dashboard.html` (custom student navbar in `nav` block, modals in `modals`, JS in `scripts`)
- `templates/admin_settings.html`
- `templates/admin_timetable.html`
- `templates/admin_staff.html`
- `templates/admin_face_attendance.html`
- `templates/admin_electives.html`
- `templates/admin_notifications.html`
- `templates/admin_audit.html`
- `templates/admin_calendar.html`
- `templates/add_student.html`
- `templates/students_list.html`
- `templates/edit_student.html`

**Still standalone by design / not migrated:**

- `templates/login.html` (custom full-screen login shell)

## 4. Verified Behavior and Tests Already Run

Run with `python -m py_compile app.py` (fast) and Flask test-client smoke tests (slow import; use timeout 300000 ms).

Verified recently:

- `python -m py_compile app.py model.py` passes.
- Notification worker thread starts at app import.
- `/sw.js` returns 200 with `Service-Worker-Allowed: /`.
- `/api/push/public_key` returns `ok: false` when VAPID keys are absent (expected).
- Student dashboard returns 200 and includes `erp.js`, `student_dashboard.js`, push and read-all controls.
- Inactive student session:
  - page request redirects to `/login`
  - API request returns 401 with `Account is inactive. Contact the administrator.`
- Active admin request returns 200 with correct admin user id.
- All main pages return 200 after base migration:
  - Admin: `/`, `/add_student`, `/admin/students`, `/admin/students/7/edit`, `/admin/staff`, `/admin/calendar`, `/admin/timetable`, `/admin/electives`, `/admin/notifications`, `/admin/settings`, `/admin/audit`, `/staff_monitor`, `/reports`, `/attendance_record`, `/take_attendance`, `/admin/face_attendance`, `/od`, `/medical`, `/profile`
  - Staff: `/staff_monitor`, `/reports`, `/attendance_record`, `/take_attendance`, `/od`, `/medical`, `/profile`
  - Student: `/student_dashboard`, `/profile`, `/od`, `/medical`
- `/api/timetables` returns both classes when called as admin.
- Student `/api/student/dashboard` returns 200.
- Temporary elective mapping test confirmed the student timetable displays the selected option. The temporary row was deleted after the test.
- Elective integration test with a temporary `student_electives` row plus temporary attendance session/entry:
  - roster returned `selected_elective` and `effective_subject`
  - analytics subject aggregation used the mapped elective label
  - period CSV included `SelectedElective`
  - temporary session, entry, and mapping were deleted afterward
- Settings page and API were smoke-tested:
  - `GET /admin/settings` 200
  - `GET /api/admin/settings` 200 with cutoff `18`, threshold `75`, calendar enforcement `0`
- Staff management API previously returned 19 staff / 19 timetable staff / 2 advisors.
- Elective API previously returned:
  - `24IT5A`: 7 slots, 0 students
  - `24IT5B`: 6 slots, 1 student
- Notification admin API previously returned config all false because SMTP/Fast2SMS/VAPID are unset.
- Local Phase 5 smoke test passed `46/46` on a temporary DB with a fixed IST clock (`2026-11-16 12:00`). It covers certificate validation, one-active-request enforcement, advisor/HOD scoping and approvals, due-date crediting, medical locks, dashboard/CSV crediting, certificate access, cancellation/reversal, rejections, future deferral, history scoping, and page/nav rendering.
- Local Phase 6 smoke test passed `36/36` on a temporary DB with recognition monkeypatched for deterministic faces and a generated AVI video. It covers admin-only page/API access, model/limit status, webcam job start, known/unknown capture and dedupe, unknown assignment, stop/review, face-source attendance submit, session/entry confidence and flagged flags, video upload/background status, multi-face aggregation, crop serving, 550 MB limit configuration, video existence, deletion after completion, and job cleanup.
- Local Phase 7 scale check passed `29/29` on a temporary DB with 75 synthetic students and deterministic monkeypatched detections. It covers the 75-row roster, 80-face worker setting, 60 unique matched students, unknown dedupe/cap, 75-row attendance submission, video deletion after completion, stale-video TTL deletion, unsupported-format rejection, and zero test-file leftovers.
- Wave 1 smoke test passed `29/29` on a temporary DB. It covers admin page rendering, staff create/profile persistence/deactivate/reactivate/archive and login enforcement, student role storage for Representative/Ambassador/custom/blank roles, `rep_badge()` behavior, settings metadata and clamps, and audit filters/search/elective compatibility.
- Wave 1 guarded real-DB migration completed with backup `C:\Users\alagu\AppData\Local\Temp\opencode\wave1_attendance_backup_pre_migration.db`; it added the Wave 1 user profile columns, `students.student_role`, and audit indexes. Existing four students currently have blank roles.
- Re-run after Wave 1: `phase1_redesign` all passed; Phase 3 `17/17`, Phase 4 `23/23`, Phase 5 `46/46`, Phase 6 `36/36`, and Phase A/B `33/33` passed. Phase 7 reported `28/29` only because its final assertion expects the shared face-crop upload directory to be empty and pre-existing crop files were present. `phase_cd_smoke` reported `18/19` only because its copied real DB contains an overlapping `2025-08-01..2025-12-31` semester; a clean probe confirmed `attendance_edit_allowed()` correctly closes after `2025-09-01`. Neither failure is a Wave 1 code regression.
- Wave 2 smoke test passed `28/28` on a fresh temp DB. It covers setting defaults/clamps, status normalization and helpers, analytics `-`/`N` behavior, 06:00/day-15/future window boundaries, unmarked reminder creation and staff assignment, reminder read/read-all endpoints, digest updates and cleanup, admin settings, calendar end-date validation, timetable reference class validation, and elective version validation.
- Wave 2 guarded real-DB migration completed with backups `C:\Users\alagu\AppData\Local\Temp\opencode\wave2_attendance_backup_pre_migration.db` and `C:\Users\alagu\AppData\Local\Temp\opencode\wave2_attendance_backup_pre_migration_20261118_225752.db`. It converted `2572` `source='auto_absent'` `Absent` entries to `-`, preserved four manual `Absent` entries, sanitized auto-created session labels, added `staff_alerts`, and set attendance window settings to `06:00`, `15` days, `22:00`.
- Re-run after Wave 2: Wave 1 `29/29`, timetable `15/15`, elective migration `1/1`, electives `17/17`, insights `23/23`, medical `46/46`, face `36/36`, and Phase A/B `33/33` passed. Two smoke-script expectations were updated for the approved Wave 2 policy: medical cancellation reverts to `-` instead of `Absent`, and the dashboard synthetic-cell check accepts `-`/`N`.
- Wave 3 webcam reliability is implemented: concurrent `getUserMedia` calls are coalesced, camera starts wait for a real video frame, overconstrained devices fall back to default camera constraints, permission/busy/disconnect/tab-hidden errors show actionable status text, capture reconnects a dropped stream, and streams stop when a job closes/finishes or the page unloads.
- Wave 3 model support is implemented with graceful fallback: YuNet ONNX is used when present, SFace ONNX can be enabled with `ERP_FACE_RECOGNIZER_MODEL` or local model files, and trained embedding mode is recorded in `model.meta.json`. Existing installations without new model assets keep Haar detection and 1024-dimensional grayscale embeddings, so no model/DB migration is required.
- `/api/admin/face/status` now returns `models` with detector, embedding, asset availability, resolved model path, and saved metadata; the admin face badge shows the active detector/embedding mode.
- Wave 3 verification: `python -m py_compile app.py model.py`, `git diff --check`, `node --check static/js/admin_face_attendance.js`, `wave3_model_smoke.py` `6/6`, face `36/36`, Wave 2 `28/28`, Wave 1 `29/29`, timetable `15/15`, electives `17/17`, insights `23/23`, medical `46/46`, and Phase A/B `33/33` passed. Phase 7 reported `28/29` and Phase C/D `18/19` only for the known pre-existing crop-file and overlapping-real-DB smoke caveats.
- Remaining Wave 3 verification is manual browser camera-permission/stream lifecycle testing on the deployment machine and real classroom recognition accuracy testing when model assets/footage are available.
- Wave 4 CSV exports now honor the active class/semester/date/absence filters and apply the same staff-handled scoping as on-screen analytics; CSV column shapes and role-export policy are unchanged.
- Wave 4 report export buttons are wired from the active filters, the Records link carries custom date ranges, and Records accepts `from`/`to` inputs with explicit dates taking precedence over the selected period/semester.
- Wave 4 Records now shows credited and `Absent/N` wording, consistent status badges for `N`/`-`/Medical Leave, the computed elective fallback, and a refreshed policy note. Defaulters CSV headers now read `Present/ML` and `Absent/N`.
- Wave 4 report/record period bounds and OD modal date defaults use IST; invalid or reversed ISO date ranges are ignored safely.
- Wave 4 adds print rules for reports, records, and the student grid, plus a student dashboard footer update and subject-bar colors based on the configured threshold.
- Wave 4 verification: `python -m py_compile app.py model.py`, `git diff --check`, `node --check static/js/student_dashboard.js`, `wave4_smoke.py` `18/18`, Wave 2 `28/28`, Wave 1 `29/29`, redesign check all passed, timetable `15/15`, elective migration `1/1`, electives `17/17`, insights `23/23`, medical `46/46`, face `36/36`, and Phase A/B `33/33` passed. Phase 7 reported `28/29` and Phase C/D `18/19` only for the known pre-existing crop-file and overlapping-real-DB smoke caveats.
- Remaining Wave 4 verification is manual browser CSV comparison, filter carry-over, print preview, mobile layout, and camera/accuracy checks on the deployment machine.

Important current local data state:

- Classes: `24IT5A`, `24IT5B`
- Current students: `Meyyappan AL` (id `7`, `24IT5B`), `Alagappan AL` (id `8`, `24IT5A`), `Unnamalai` (id `9`, `24IT5B`)
- Admin user: username `ADMIN`, id `23` (query DB before relying on id; seed/reset can change it)
- Student login user: id `24`, username `732924ITR320` (query DB before relying on id)
- `student_electives` currently has no temporary test row.

Do not hardcode user ids in application code. Test scripts should query the DB if they need an id.

## 5. Important Environment Quirks

- Importing `app` is very slow in this environment because of heavy imports (`cv2`, `scikit-learn`). Always allow up to 300000 ms for Flask test-client scripts.
- `python -m py_compile app.py` is fast and should be run after every Python change.
- PowerShell does not provide `rg`. Use the `grep` tool for content search.
- Timetable JSON files currently contain a UTF-8 BOM. `load_timetable()` correctly opens with `encoding='utf-8-sig'`. Do not switch it back to plain `utf-8`.
- `.env` is not configured with real credentials. Notification sends are expected to be disabled or fail with clear messages until credentials are provided.
- `pywebpush` must be installed for Web Push sending. It is listed in `requirements.txt`.
- The notification worker starts at module import. It now has an in-process start guard and a Flask debug-reloader guard (`WERKZEUG_RUN_MAIN`). A DB-level atomic claim (`claimed_at` + compare-and-swap on `attempts`) now prevents duplicate sends across multiple WSGI workers; stale `processing` rows are reclaimed after 10 minutes.
- The absence/OD worker also starts at import. In `__main__` it waits for `WERKZEUG_RUN_MAIN='true'` so the debug reloader does not run it twice. Disable with `ERP_ABSENCE_WORKER=0` (both smoke tests do this).
- Default-Absent materialization commits per date while backfilling, so a large first pass does not hold the SQLite write lock for the whole semester.

## 6. Pending Work / Known Gaps

### A. Batch-semester Phase 5 hardening
- Run guarded migration smoke on a fresh copy of real `attendance.db` (backup first, temp DB path, workers off); verify S1-S8 per batch, cohort rows, no duplicate batch-sem rows, and key routes load.
- Audit any remaining user-facing global-semester assumptions and replace with batch-sem context where needed.
- Manual browser QA for batch-sem UI (semesters, timetable, electives, reports/records/student dashboard/admin filters).

### B. Base template and navigation cleanup

- Main pages are now migrated to `base.html` and return 200 for Admin, Staff, and Student roles.
- Remaining standalone templates:
  - `login.html` (custom full-screen auth shell; keep standalone unless a clear need arises)
- Removed the unused legacy partials `erp_shell_top.html`, `erp_shell_bottom.html`, and `erp_flash.html` during Phase 7 cleanup.
- Verify modals and dropdowns in a real browser. Server-rendered 200 does not prove Bootstrap JavaScript behavior.

### B. Encoding and legacy artifact cleanup

- Main pages (`index`, `staff_monitor`, `od`, `student_dashboard`, and migrated admin pages) were cleaned of corrupted display text.
- No literal `` `r`n `` artifacts were found after cleanup.
- Legacy `mark_attendance.html`, `camera_mark.js`, and `camera_signout.js` were removed in Local Phase 1. `/mark_attendance` remains as a redirect to `/take_attendance` for old bookmarks.
- Hardcoded class names remain only as example/help text in `add_student.html`.
- Keep example class names in help text only; avoid hardcoded dropdown options.

### C. Elective integration

- Done: student timetable/dashboard displays mapped elective options.
- Done: attendance roster API returns `selected_elective` and `effective_subject` per student for option slots.
- Done: take-attendance roster shows an `Elective` column.
- Done: attendance records log shows an `Elective` column.
- Done: reports `/api/analytics/overview` subject aggregation uses the studentâ€™s selected elective where mapped.
- Done: `/download_period_csv` appends `SelectedElective`.
- Done: `/download_defaulters_csv` appends an `Electives` column.
- Existing attendance sessions still store the timetable base subject; per-student display is resolved through `student_electives` and the entry-level `elective_label` snapshot.
- Done: mappings are now scoped to timetable versions, and attendance entries store the effective elective label at mark time. Older entries without labels fall back to the version-aware mapping for their date, then to legacy NULL-version mappings.
- Remaining: none for historical elective labels; new writes should keep populating `elective_label`.

### D. Notification delivery end-to-end

- Done: multi-worker hardening.
  - `notification_outbox` has a `claimed_at` column (added via `_ensure_column`).
  - `process_notification_outbox()` atomically claims each row (`status='processing'`, `attempts+1`, `claimed_at`) using a compare-and-swap on `attempts`, so concurrent workers cannot send the same item twice.
  - Stale `processing` rows older than 10 minutes are reclaimed as `failed` so a crashed worker cannot strand an item.
  - `process_notification_outbox(only_ids=[...])` was added, and the admin retry route now retries the requested row (previously it reset one row but processed the oldest available row).
- Verified without real credentials:
  - Fresh `processing` claim is not stolen.
  - Stale claim recovery retries the row.
  - Two concurrent workers process a row exactly once (delayed-send test).
  - Retry endpoint re-attempts only the requested row; unrelated pending rows are untouched.
  - Email/SMS test-send returns a clear 400 error when SMTP/Fast2SMS are unconfigured, and the failure is stored in `last_error`.
  - Student single-alert read and read-all acknowledgement both mark `read_at`.
- Remaining: configure `.env` with real SMTP/Fast2SMS/VAPID values and confirm actual delivery, worker processing, and push subscription cleanup in a real browser. Add a DB-level outbox claim is now done (see above).
- Security note: `attendance.db` is currently tracked by git even though `.gitignore` lists it. Untrack it (`git rm --cached attendance.db`) before any commit if the runtime DB should stay local.

### E. Account and role edge cases

- Active-account checks now query the DB on every protected request. Confirm this is acceptable for expected load or add a short-lived cache.
- Verify admin editing a staff/HOD account does not unintentionally demote roles or reset unrelated fields.
- Verify password reset and forced login behavior for inactive/reactivated accounts.
- Confirm old sessions without `user_id` are handled safely.

### F. Final quality pass

- Smoke-test all pages as Admin, Staff, HOD/Advisor, and Student.
- Verify every admin module page loads its JavaScript after base migration.
- Verify no duplicate script includes remain after migration.
- Check mobile navbar collapse, active link states, and modals.
- Run a final encoding scan.
- Only commit after explicit user request.

## 7. Next Step: To-Do Phase 1

Phase 1 is the remaining cleanup and integration work needed to make the ERP uniform and continue safely.

### Phase 1.1 - Base template migration (completed)

Completed pages:

1. `index.html`
2. `staff_monitor.html`
3. `reports.html`
4. `attendance_record.html`
5. `take_attendance.html`
6. `od.html`
7. `profile.html`
8. `student_dashboard.html`

Notes:
- `student_dashboard.html` keeps its custom student navbar via `{% block nav %}`, its styles in `{% block head %}`, and `student_dashboard.js` in `{% block scripts %}`. Its modals are in `{% block modals %}`.
- `login.html` remains standalone by design.
- `mark_attendance.html` remains legacy and standalone.

**Acceptance criteria met:** all main pages return 200 for Admin, Staff, and Student; duplicate Bootstrap/ERP includes were removed; `py_compile` passes. Browser-level modal/dropdown verification is still recommended.

### Phase 1.2 - Encoding/artifact sweep (completed)

- Corrupted text in main pages was cleaned.
- No literal `` `r`n `` artifacts remain in templates.
- Main pages now use dynamic class lists.
- Only example class names remain in help text.
- `mark_attendance.html` contains valid Unicode symbols, not broken UTF-8; leave it or remove the legacy page deliberately.

**Acceptance criteria met for active pages.** A browser-level visual check is still recommended.

### Phase 1.3 - Elective integration (completed)

- Added `_elective_lookup()` and `_effective_subject()` helpers.
- Attendance roster returns selected/effective elective per student.
- Take-attendance roster displays an `Elective` column.
- Attendance records log displays an `Elective` column.
- Analytics subject aggregation uses effective elective labels.
- Period CSV appends `SelectedElective`; defaulters CSV appends `Electives`.
- Verified with a temporary student_electives row and temporary attendance session/entry; both were removed afterward.

**Acceptance criteria met.** The historical-reporting concern was resolved in Local Phase 3 by scoping mappings to timetable versions and snapshotting `attendance_entries.elective_label` at mark time.

### Phase 1.4 - Notification end-to-end and production hardening (hardening done; real-channel delivery pending credentials)

Completed:
- Added DB-level outbox claim (`claimed_at` column + atomic compare-and-swap on `attempts`) and 10-minute stale-claim recovery.
- Fixed the admin retry route so it processes the requested outbox row via `process_notification_outbox(only_ids=[...])` instead of the globally oldest row.
- Verified with a 16-check script (temporary rows removed afterward):
  - schema/columns, fresh-claim protection, stale-claim recovery, concurrent single-send, retry isolation, clear email/SMS config errors, and student read/read-all ack.
- Confirmed no `.env` file is present/tracked and `.env.example` contains placeholders only.

Still pending (blocked on real credentials):
- Fill `.env` from `.env.example` in a local environment only.
- Test SMTP, Fast2SMS, and Web Push with real credentials.
- Confirm push subscription cleanup for expired endpoints in a real browser.
- Verify the background worker with configured channels.

**Acceptance criteria:** at least one real channel sends successfully in a controlled test; failures are visible and retryable. Failure/retry visibility and retry isolation are verified; real send remains blocked until credentials are provided.

### Phase 1.5 - Final regression and handoff (server-side completed; browser pending)

Completed:
- `python -m py_compile app.py` passes.
- Flask smoke tests pass for Admin, Staff, and Student pages.
- Key APIs return 200:
  - `/api/admin/settings`
  - `/api/admin/electives?class=24IT5B`
  - `/api/admin/notifications`
  - `/api/student/dashboard`
  - `/api/medical/my`
  - `/api/timetables`
  - `/sw.js`
- Migrated pages include exactly one Bootstrap bundle and one `erp.js` reference.

Still pending:
- Browser-level console check and visual check of modals/dropdowns/nav collapse.
- `PROJECT.md` should be updated after the browser pass.

## 7A. Next Step: Phase 2 (Attendance correctness and camera)

### Phase 2A - Role-aware navigation (completed)

- `templates/partials/erp_nav.html` now branches on role instead of treating every non-admin as staff.
- Students get Dashboard / My Academics / OD / Alerts / Profile / Logout; `/od` and `/profile` no longer show staff links to students.
- Staff/HOD get Dashboard / Take Attendance / Records / OD / Reports. Admin additionally gets Semesters.
- Verified via Flask test client: student `/student_dashboard`, `/od`, `/profile`; admin `/admin/semesters`, reports, records.

### Phase 2B - Semesters and full-grid attendance stats (completed)

- Added `semesters` table: `label`, `start_date`, `end_date`, `is_current`, audit fields. Seeded from `app_settings` when empty.
- Added `/admin/semesters` page and `/api/admin/semesters` (GET/POST/DELETE). One current semester is enforced; legacy settings sync with the current semester.
- Added `/api/semesters` for the student/staff dropdowns.
- `api_student_dashboard` now:
  - resolves a selected `semester_id` (current = start..today, completed = start..last scheduled working day, holidays skipped, `working_day_override` honored; if a semester has no start date, falls back to the earliest session date in the last 180 days, else today);
  - builds the full scheduled date x period grid for the semester;
  - marks unmarked periods whose slot ended as default `Absent` (`source='auto_absent'`), while today's future periods stay `Not Marked`;
  - computes working days, present days, absent days, scheduled periods, counted periods, OD/Late, auto-absent, and percentage;
  - returns semester options and full class/slot/source data for the detail modal.
- Staff `/reports` and `/attendance_record` accept a semester dropdown. Reports resolve the semester window server-side.
- Student dashboard UI: semester selector, concise semester range, calendar moved to a collapsed "Upcoming academic calendar" panel, and cleaned marking-log modal.

Verification: `python -m py_compile app.py` passes; a 33-check temporary-DB smoke test passed (student dashboard keys/grid, synthetic cell class, role nav, admin semester create/delete, reports/records semester dropdowns).

### Phase 2C - Default-Absent persistence and edit windows (completed)

- `_materialize_default_absent(class_name=None, date_from=None, date_to=None, conn=None, semester=None)` persists `Absent` rows (`source='auto_absent'`) for timetable periods whose slot has ended and which have no entry yet.
- Background `absence-worker` starts at app import (`_start_absence_worker`), backfills the current semester once, then re-checks today every 60s. Disable with env `ERP_ABSENCE_WORKER=0` (smoke tests do this).
- Materialization is idempotent (`ON CONFLICT DO NOTHING`), skips holidays unless `working_day_override`, and never overwrites explicit Present/Late/OD/manual marks.
- `attendance_edit_allowed` now blocks all future attendance. Staff may change marks only on the same IST date before `attendance_cutoff_hour` (default 18:00); HOD/Admin may correct after cutoff only until the semester end date. After semester end, manual changes are closed.
- `/api/attendance/roster` returns `edit_allowed`/`edit_reason`; `take_attendance.html` disables roster/submit controls when the window is closed.
- Removed the old "Previously Absent: only subject/handling staff" submit block; `can_mark_period` still enforces timetable/advisor/HOD access.

### Phase 2D - OD day-of application (completed)

- `_apply_approved_od` now applies only dates already due; future approved dates are deferred.
- `_apply_due_approved_od` runs in the absence worker and applies every `od_approved` request date-by-date at/after 06:00 IST.
- Same-day HOD approval after 06:00 applies immediately. Approval before 06:00 waits for the worker pass.
- OD writes only scheduled periods, skips holidays (unless override), and never overrides explicit Present/Late.
- Fixed a latent SQL binding bug in `_find_or_create_session` that made OD auto-created sessions fail silently (11 params for 10 placeholders).
- Verification: `python -m py_compile app.py` passes; new 19-check `phase_cd_smoke.py` passed (default-Absent persistence/idempotency, Present precedence, due/future OD, 06:00 IST boundary, edit-window and semester-end rejection). Existing 33-check Phase A/B smoke still passes.

### Phase 2E - Cloud and CCTV (deferred)

- Cloud hosting, Supabase/Postgres, Cloudflare R2, and live CCTV/RTSP are deferred until staff approval.
- Admin webcam and uploaded-video attendance remain in scope and run locally.
- Camera/video target a selected class/date/period and submit into the existing period attendance session.
- Unrecognized students remain unmarked in the draft; admin may override before submit.
- Recognition must handle 60+ students per class. Upgrade to SFace if the current model is insufficient.
- Video upload max 550 MB, processed by a local background job, then deleted after review.

## 7B. Local-Only Delivery Phases

### Local Phase 1 - Manual attendance hardening (completed)

- Added admin `Take Attendance` links to the role-aware nav and admin dashboard.
- Admin uses the existing timetable roster with `Admin (universal)` access and edit-window overrides.
- Removed orphaned legacy camera/sign-out UI: `mark_attendance.html`, `camera_mark.js`, `camera_signout.js`, and dead monitor controls in `admin_dashboard.js`.
- `/mark_attendance` still redirects to `/take_attendance`.
- Verification: admin dashboard/nav links, timetable cell, roster, submit, and `Present/manual` entry all passed on a temporary DB copy.

### Local Phase 2 - Timetable versions + PDF/image upload + manual editor (completed)

- Added `timetable_versions` (`class_name`, `semester_id`, `version_no`, effective dates, JSON snapshot, reference path/name, active flag, notes, creator/timestamps).
- Existing `timetables/<class>.json` files are seeded as version 1 for the current semester at startup.
- `load_timetable(class, date)` now resolves the version effective on that date and falls back to the legacy JSON file.
- `timetable_cell()` and default-Absent materialization resolve per date, so historical versions drive the correct periods.
- Attendance sessions now record `timetable_version_id` and `duration_minutes` for manual, auto-absent, and OD-created sessions.
- Added admin APIs for version list/detail/save/restore and PDF/JPG/PNG/WEBP reference upload/serve.
- Saving an existing effective date updates that version in place; later dates create a new version and close the previous one. Restoring a historical version creates a new version effective from the chosen date.
- Replaced the raw JSON upload page with a manual grid editor: advisor, hall, periods/times, subject/staff cells, elective options, staff directory, version notes, reference upload, raw JSON fallback, and version history.
- Admin dashboard link now says `Timetable Versions & Manual Editor (PDF/image reference)`.
- No OCR initially.
- Verification: 15/15 Phase 2 checks passed on a temporary DB copy (seed, create class, save/update v1, versioned v2, date resolution, reference upload/serve, session version/duration, admin page).

### Local Phase 3 - Electives + shared roster (completed)

- Added nullable `student_electives.timetable_version_id`; unique key is now `(student_id, class_name, timetable_version_id, slot_key)`. Existing rows migrate to NULL-version legacy mappings and remain usable as fallback.
- Added `attendance_entries.elective_label`; manual submit, default-Absent materialization, and OD application snapshot the effective option for the entry.
- Elective lookup is version/date-aware: version-specific mapping first, then legacy NULL-version mapping. Reports/dashboard prefer stored entry labels, then version-aware mapping.
- Both elective staff can mark the full shared class roster; `_is_assigned_staff` also recognizes staff codes embedded in elective option labels.
- Admin elective page now supports class + semester + timetable-version selection, KPI counts (mapped/missing/unassigned/conflicts), legacy-vs-version source badges, version-scoped CSV import/export, carry-forward from the previous version, and recent elective audit entries.
- New API: `POST /api/admin/electives/carry-forward`. Existing GET/POST/export/import accept `semester_id` and `version_id`.
- Verification: 17/17 Phase 3 checks passed on a temporary DB copy (schema/columns, version-scoped save/lookup, legacy isolation, shared ES/TK roster, manual and auto-Absent labels, historical label preference, carry-forward, admin page). A separate migration check confirmed an old elective row survives with `timetable_version_id=NULL` and the new index. Phase 2 15/15, Phase 1 19/19, Phase A/B 33/33, and Phase C/D 19/19 still pass.

### Local Phase 4 - Attendance views, comparisons, and absence filters (completed)

- Added `GET /api/analytics/insights` with role-scoped filters, status totals, defaulters, comparison rows, and absent-period/hour metrics.
- Admin insights cover all matching sessions; staff insights cover only sessions where `attendance_sessions.staff_code` or `staff_name` matches the logged-in staff. Staff cannot request staff/period comparisons (coerced to class) or read another class outside their handled sessions.
- Comparison modes: class/class, staff/staff, student/student, period/period. Non-admin staff only get class and student modes.
- Absence filters: `any`, `full_day`, `partial_day`, `period_wise`, `none`. Classification runs per student/date after class, semester, and date filters:
  - full-day = every row for that student/date is Absent;
  - partial-day = some Absent and some non-Absent;
  - period-wise = only Absent rows;
  - none = student/dates with no Absent row.
- Reports page now has semester/class/date/absence filters, six summary cards, status donut, comparison bar chart/table, searchable student attendance table, and retained subject/elective and compliance tables.
- `attendance_record` now has the same absence dropdown, filters both the period log and student summary, and is staff-scoped. `/api/analytics/overview` is also staff-scoped.
- Metrics include Present, Absent, Late, OD, Medical, absent periods, absent hours (session `duration_minutes`, fallback 60), credited percentage, defaulters, full-day absences, and partial-day absences.
- No schema change.
- Verification: 23/23 Phase 4 checks passed on a temporary DB copy (admin/staff scoping, all comparison modes, every absence filter, absent hours/percentage, page controls, overview/record scoping). Phase 3 17/17, Phase 2 15/15, Phase 1 19/19, Phase A/B 33/33, Phase C/D 19/19, migration check, and `py_compile` still pass.

### Local Phase 5 - Medical leave + student analytics (completed)

- Added `medical_requests` and `medical_records`, with one active request per student/semester and resubmission allowed after rejection/cancellation.
- Full-day is the default; optional selected periods are supported. A certificate is mandatory (`.pdf`, `.jpg`, `.jpeg`, `.png`, `.webp`) and stored under `uploads/medical_certificates/`.
- Workflow is Advisor â†’ HOD. Approved leave applies date-by-date when due, waits for future dates, credits `Medical Leave`/`medical_workflow`, and preserves explicit Present/Late/OD. HOD can cancel approved leave until the period is over; cancellation reverts credited entries to Absent. Students can cancel pending/advisor-approved requests.
- `/medical` provides student submission/history, advisor/HOD review queues, certificate access, and role-scoped history. Admin can view history/certificates for audit but does not approve. Advisor contact is a contact card only; no online notifications.
- Take Attendance shows `Medical Leave` and locks medical entries for non-HOD/Admin staff. HOD/Admin may correct them while the edit window allows.
- Student dashboard analytics include goal calculator, monthly heatmap, 75% threshold warning, subject/elective breakdown, today/next class, and CSV export. Medical Leave counts as credited attendance. `/student/export_csv` was added.
- Verification: `phase5_medical_check.py` passed `46/46` on a temporary DB with a fixed IST clock. It covers validation, active-request enforcement, role scoping, approvals, due/future application, locks, dashboard/CSV crediting, certificates, cancellation/reversal, rejection, history, and page/nav rendering.

### Local Phase 6 - Admin webcam and video attendance (completed)

- Added `face_attendance_jobs` and `face_attendance_faces` schema plus `uploads/face_attendance/videos/` and `uploads/face_attendance/crops/`.
- Admin-only `/admin/face_attendance` page with a selected class/date/period context, webcam capture (one student at a time), classroom video upload, job history, and a draft review table.
- Webcam flow starts a `recording` job, captures one frame on demand, matches only students in the selected class, and updates the same draft row per student. Unknown captures use a single overridable unknown row until assigned.
- Video flow accepts the existing allowed formats up to `ERP_FACE_VIDEO_MAX_MB` (default 550 MB, upload limit 575 MB), saves a unique file, and processes it in a local background thread at `ERP_FACE_SAMPLE_FPS` (default 2 fps).
- Video processing deduplicates known students by student id and unknown faces by embedding cosine similarity, keeps the highest-confidence crop, flags low-confidence known faces (`< ERP_FACE_REVIEW_CONFIDENCE`, default 0.72), and keeps unrecognized faces as unknown rows.
- Review supports manual unknown-face assignment, manual status changes, search, and `All Absent`. Recognized students default to Present only when the roster status is Absent/Not Marked; approved OD/Medical and existing Present/Late are preserved. Unassigned unknown faces stay unmarked.
- Submission uses the existing `/api/attendance/submit` validation and writes `attendance_sessions.source=face_attendance` when source is supplied by an admin. Recognized entries store `source=face_attendance`, confidence, and flagged; other rows stay manual. Duplicate OD rows are now suppressed.
- Uploaded videos are deleted on review completion, explicit failure, interrupted-job recovery at startup, or after `ERP_FACE_JOB_VIDEO_TTL_HOURS` (default 24). Crops remain with the job until the job is deleted.
- Multi-face detection in `model.py` uses optional YuNet (`FACE_DETECTOR_MODEL` or `models/face_detection_yunet_2023mar.onnx`) and falls back to Haar. Recognition keeps the existing trained RandomForest model and its `model.pkl`; no missing ONNX/SFace assets are required. SFace can be added later if accuracy testing shows it is needed.
- Added JSON 413 handling for oversized uploads and admin crop serving through `/api/admin/face/crops/<face_id>`.
- Verification: `phase6_face_check.py` passed `36/36` on a temporary DB. Phase 5 46/46, Phase 4 23/23, Phase 3 17/17, Phase 2 15/15, Phase 1 19/19, Phase A/B 33/33, Phase C/D 19/19, migration check, and `py_compile` still pass. Browser-level webcam/video UI and real classroom recognition accuracy remain unverified in this environment.

### Local Phase 7 - Verification and cleanup (completed)

- Added `ERP_FACE_VIDEO_MAX_FACES` (default 80) so a sampled frame is no longer capped at 20 faces, and `ERP_FACE_UNKNOWN_MAX_GROUPS` (default 300) to bound unknown-face clustering work and memory.
- Reworked unknown-face dedupe to use cached normalized embedding matrices and vectorized similarity search instead of a Python cosine loop per cluster. New unknown clusters are not created after the configured cap.
- Exposed the video face cap and unknown-group cap through `/api/admin/face/status` and the face-attendance page config.
- Removed the unused `_face_unknown_similarity` helper and the unused legacy partials `erp_shell_top.html`, `erp_shell_bottom.html`, and `erp_flash.html`.
- Added `.env.example` entries for all local face-attendance limits and thresholds, including the optional `FACE_DETECTOR_MODEL`.
- Phase 7 scale check passed `29/29` on a temporary DB with a synthetic 75-student class, a generated video, and deterministic monkeypatched detections. It covers a 75-row roster, 80-face worker configuration, 60 unique matched students, unknown dedupe, unknown-group cap, 75-row attendance submission and entry storage, video deletion after completion, stale-video TTL deletion, unsupported-format rejection, and zero test-file leftovers.
- Full server-side regression suite still passes after the scaling changes. Remaining verification is manual browser UI (camera permission, upload progress, tabs/polling) and real classroom recognition accuracy; both require real devices/footage.

### Redesign Phase 1 - Admin/student foundation (completed server-side; manual UI pending)

- Added `batch_year` and `study_year` columns to `students`, with backfill from class; `24IT5A` maps to batch `2024-2028` and study year `3`.
- Updated `add_student`, `admin_edit_student`, `admin_students_page`, and `/students` to validate/return the new fields.
- Added read-only `/admin/students/<student_id>/attendance` with semester/date/status/period filters and total/credited/absent/percentage summary.
- Rebuilt `templates/students_list.html` with search, class/batch/year/gender/status/date filters, sorting, KPI cards, photo counts, and detail links.
- Added batch/year fields to add/edit student forms with class-based auto-fill.
- Removed the legacy admin attendance chart, staff legacy feed, and attendance raw-log UI; old DB rows remain intact.
- Verification: `python -m py_compile app.py model.py`, `git diff --check`, temp-DB `phase1_redesign_check.py`, and guarded real-DB migration all passed. Manual responsive UI checklist remains before Phase 2.

### Consolidated Wave Plan (approved)

- Wave 1 â€” foundation closeout, student role, staff management, settings/audit (completed server-side; browser verification pending).
- Wave 2 â€” academic calendar polish, timetable polish, electives polish, real `N` attendance status, hyphen `-` unmarked policy, and staff unmarked-period reminders (completed server-side; browser verification pending).
- Wave 3 â€” webcam/camera fixes and optional YuNet/SFace model upgrade (completed server-side/client-side; real browser and classroom accuracy verification pending).
- Wave 4 â€” records/reports finalization and final QA/handoff (completed server-side; manual browser CSV/print/layout verification pending).

Approved Wave 1 student-role rule:
- `student_role` is optional, selected from dropdown presets or custom text.
- Blank roles display nothing.
- Non-Representative roles appear only on Student Details.
- `Representative` renders as `Rep` beside the student name across UI surfaces.
- CSV/report exports remain unchanged.

Approved Wave 2 attendance rule (implemented):
- Replace automatic `Absent` with stored `-` rows.
- From class date `06:00` onward, scheduled unmarked periods show `-` until explicitly marked.
- Present, Absent, OD, Late, N, and Medical Leave stop `-`.
- Staff marking window: class date `06:00` to 15th day `22:00`; HOD/Admin corrections remain open until semester end.
- Existing `source='auto_absent'` rows should be converted to `-`.
- Reminders are in-app only: daily digest to assigned staff from the next day until marked, stopping after the 15-day window.
- Timetable slot staff receive the reminder; when a slot has no listed staff, fall back to the class advisor, and leave the alert unassigned for admin visibility when neither is available.
- If a digest changes (for example, one student is marked), the existing alert is updated and returned to unread; when no unmarked periods remain or the window closes, stale digests are removed.

## 8. Credentials and Seeding

Known development credentials:

- Admin: `ADMIN` / `ADMIN123` via Admin tab.
- Staff: Staff tab, password is the staff name in uppercase without titles.
  - Example HOD: `RMY` -> `MYNAVATHI`
  - Advisor 24IT5A: `KB` -> `BABY`
  - Advisor 24IT5B: `TK` -> `KOKILAVANI`
  - Another example: `TN` -> `NITHYA`
- Student: register number + DOB.
  - Current student: reg `732924ITR320`, DOB `2007-07-01`

Confirm actual passwords and ids by querying the DB if seeding has changed. Never hardcode these into production behavior.

## 9. Key Files and Responsibilities

- `app.py`: main Flask app, schema init, auth, decorators, admin/student/staff routes, attendance, OD, timetables, electives, notifications, settings, audit, reports. Phase 2C/2D additions: `_materialize_default_absent`, `_absence_worker_loop`, `_start_absence_worker`, `attendance_edit_allowed`, `_semester_end_for_date`, `_apply_od_for_date`, `_apply_due_od_for_request`, `_apply_due_approved_od`. Phase 3 additions: `_elective_map_details`, `_resolve_elective_version`, `_elective_slots_for_version`, version-aware `_elective_maps`/`_student_elective_map`/`_effective_subject`, and `elective_label` snapshots. Phase 4 additions: `_analytics_status_key`, `_analytics_blank`, `_analytics_add`, `_analytics_finalize`, `GET /api/analytics/insights`, absence classification, and staff-scoped overview/record queries. Phase 5 additions: `medical_requests`/`medical_records` schema, `_save_medical_certificate`, `_parse_medical_periods`, `_apply_medical_for_date`, `_apply_due_approved_medical`, `_revert_cancelled_medical`, medical routes, medical crediting/locks, and the student export endpoint. Phase 6 additions: `face_attendance_jobs`/`face_attendance_faces` schema, `_recognize_faces_image`, `_face_upsert_detection`, `_process_face_video_job`, `_face_job_lock`, unknown-cluster dedupe, video recovery/expiry, admin webcam/video/crop routes, and `source=face_attendance` submit support.
- `model.py`: optional face embedding/model training; `load_model_if_exists`, training, prediction, `detect_faces_bboxes` (optional YuNet with Haar fallback).
- `templates/base.html`: new shared page shell.
- `templates/partials/erp_nav.html`: new role-aware navigation.
- `templates/index.html`: admin dashboard and train-model UI; migrated.
- `templates/admin_settings.html`: semester, threshold, cutoff, calendar enforcement; migrated.
- `templates/admin_timetable.html`: timetable version/manual grid editor with PDF/image reference upload; migrated.
- `templates/admin_staff.html`: staff/HOD/advisor management; migrated.
- `templates/admin_electives.html`: version-scoped elective matrix with semester/version selectors, KPI counts, carry-forward, source badges, CSV import/export, and recent audit panel.
- `templates/admin_face_attendance.html`: admin webcam/video face attendance, job history, unknown-face assignment, and draft review.
- `static/js/admin_face_attendance.js`: webcam capture, XHR video upload with progress, job polling, draft merging, unknown assignment, and submit/complete flow.
- `templates/admin_notifications.html`: notification config/outbox/test UI; migrated.
- `templates/admin_audit.html`: audit viewer; migrated.
- `templates/admin_calendar.html`: calendar CRUD; migrated.
- `templates/add_student.html`: student creation + camera capture; migrated.
- `templates/students_list.html`: student list; migrated.
- `templates/edit_student.html`: admin student edit; migrated.
- `templates/staff_monitor.html`, `take_attendance.html`, `od.html`, `medical.html`, `profile.html`: migrated operational pages.
- `templates/medical.html`: student medical request/history, advisor/HOD review queue, certificate links, and cancel/review actions.
- `templates/reports.html`: Phase 4 insights dashboard with absence filters, summary cards, status donut, comparison chart/table, and student attendance table.
- `templates/attendance_record.html`: Phase 4 staff-scoped period log/student summary with absence filter.
- `templates/student_dashboard.html`: student dashboard with goal calculator, monthly heatmap, threshold warning, subject/elective breakdown, today/next class, and export.
- `templates/login.html`: standalone 3-tab login shell.
- `static/js/erp.js`: `erp.esc`, `erp.toast`, `erp.fetchJSON`, `erp.setLoading`, `erp.repBadge`, `erp.repText`.
- `static/js/student_dashboard.js`: dashboard data, alerts, read-all, push subscribe, goal calculator, monthly heatmap, subject/elective breakdown, threshold warning, and export binding.
- `static/sw.js`: service worker for Web Push.
- `static/css/erp.css`: ERP design system.
- `requirements.txt`: Python dependencies.
- `.env.example`: Flask, worker, face-attendance, notification, and push placeholders.
- `install.sh`, `run.sh`: Unix helper scripts.

## 10. Important Routes / APIs

- Auth:
  - `/login` (3-tab JSON login)
  - `/logout`
- Admin pages:
  - `/`
  - `/add_student`
  - `/admin/students`
  - `/admin/students/<id>/edit`
  - `/admin/staff`
  - `/admin/calendar`
  - `/admin/timetable`
  - `/api/admin/timetable/versions` (GET list, POST save)
  - `/api/admin/timetable/versions/<id>` (GET detail)
  - `/api/admin/timetable/versions/<id>/restore` (POST)
  - `/api/admin/timetable/reference` (POST upload)
  - `/admin/timetable/reference/<version_id>` (serve reference)
  - `/admin/electives`
  - `/admin/notifications`
  - `/admin/settings`
  - `/admin/audit`
- Admin face attendance:
  - `/admin/face_attendance`
  - `/api/admin/face/status`
  - `/api/admin/face/jobs` (GET list, POST video upload)
  - `/api/admin/face/jobs/<id>` (GET detail, DELETE job)
  - `/api/admin/face/webcam/start`
  - `/api/admin/face/jobs/<id>/frame`
  - `/api/admin/face/jobs/<id>/stop`
  - `/api/admin/face/jobs/<id>/assign`
  - `/api/admin/face/jobs/<id>/complete`
  - `/api/admin/face/crops/<face_id>`
- Staff/attendance:
  - `/staff_monitor`
  - `/take_attendance`
  - `/attendance_record`
  - `/reports`
  - `/api/timetables`
  - `/api/timetable/cell`
- Student:
  - `/student_dashboard`
  - `/api/student/dashboard`
  - `/student/export_csv`
  - `/student_notifications`
  - `/api/student/alerts/<id>/read`
  - `/api/student/alerts/read_all`
  - `/profile`
  - `/od`
  - `/medical`
- Medical:
  - `/api/medical/request`
  - `/api/medical/my`
  - `/api/medical/cancel`
  - `/api/medical/pending`
  - `/api/medical/review`
  - `/api/medical/certificate/<rid>`
  - `/api/medical/history`
- OD:
  - `/api/od/request`
  - `/api/od/my`
  - `/api/od/pending`
  - `/api/od/review`
  - `/api/od/history`
  - `/api/od/proof/<id>`
- Notifications:
  - `/api/admin/notifications`
  - `/api/admin/notifications/settings`
  - `/api/admin/notifications/process`
  - `/api/admin/notifications/test`
  - `/api/admin/notifications/retry/<id>`
  - `/api/push/public_key`
  - `/api/push/subscribe`
  - `/api/push/unsubscribe`
  - `/sw.js`
- Settings/audit:
  - `/api/admin/settings`
  - `/api/admin/audit`
- Electives:
  - `/api/admin/electives` GET/POST (`class`, optional `semester_id`, `version_id`)
  - `/api/admin/electives/carry-forward` POST
  - `/api/admin/electives/export` (`class`, optional `semester_id`, `version_id`)
  - `/api/admin/electives/import` (multipart `class_name`, optional `semester_id`, `version_id`)

## 11. Useful Verification Commands

Run from project root:

```powershell
python -m py_compile app.py
```

Smoke test after import (allow 300000 ms):

```powershell
@'
import app
c = app.app.test_client()
with c.session_transaction() as s:
    s['logged_in'] = True
    s['role'] = 'admin'
    s['username'] = 'ADMIN'
    s['user_id'] = 23
for p in ['/', '/admin/settings', '/admin/timetable', '/admin/staff', '/admin/electives', '/admin/notifications', '/admin/calendar', '/admin/audit', '/admin/students']:
    r = c.get(p)
    print(p, r.status_code, len(r.data))
'@ | python -
```

Check actual admin/student user ids:

```powershell
@'
import sqlite3, app
con = sqlite3.connect(app.DB_PATH)
c = con.cursor()
print(c.execute("SELECT id,username,role,active FROM users WHERE role='admin'").fetchall())
print(c.execute("SELECT id,username,role,student_id,active FROM users WHERE role='student'").fetchall())
con.close()
'@ | python -
```

Find legacy artifacts:

```powershell
Select-String -Path templates\*.html -Pattern '24IT5A|24IT5B'
```

Use the `grep` tool for deeper searches.

## 12. How to Continue in a New Chat

1. Read this file first.
2. Run `python -m py_compile app.py`.
3. Run the admin page smoke test above with a real admin user id from the DB.
4. Continue in this order:
   - Local Phase 2: timetable versions, PDF/image upload, and manual editor (completed)
   - Local Phase 3: version-scoped elective mapping, immutable labels, and shared 60-student roster (completed)
   - Local Phase 4: attendance views, comparisons, absence filters, and donut charts (completed)
   - Local Phase 5: medical leave and student analytics (completed)
   - Local Phase 6: admin webcam/video attendance (completed; SFace optional if real accuracy testing requires it)
   - Local Phase 7: verification and cleanup (completed server-side; manual browser/real-footage checks remain)
   - Redesign Phase 1: admin/student foundation (completed server-side; manual responsive UI checklist pending)
   - Wave 1: student role, staff management, settings/audit (completed server-side; manual browser verification pending)
   - Wave 2: `N` status, `-` unmarked policy, staff reminders, calendar/timetable/electives polish (completed server-side; manual browser verification pending)
   - Wave 3: webcam/camera reliability fixes and optional YuNet/SFace support with graceful fallback (completed; real browser and classroom accuracy verification pending)
   - Wave 4: records/reports export filter parity, staff scoping, custom date ranges, status wording, IST handling, and print rules (completed server-side; manual browser CSV/print/layout verification pending)
   - Next: manual browser verification of Waves 1-4 and real classroom recognition accuracy when data is available
   - Deployment-machine/browser verification and real classroom accuracy testing remain when data is available
   - Cloud/R2/Postgres/CCTV remain deferred until staff approval
   - Real-channel notification delivery still requires SMTP/Fast2SMS/VAPID credentials
5. Do not commit unless the user explicitly asks.
6. Update this file whenever the project state or next-step order changes.

