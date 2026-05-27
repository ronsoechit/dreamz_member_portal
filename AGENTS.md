# Dreamz Fitness Portal - Codex Project Instructions

## Product context
This project is the Dreamz Fitness Bonaire member portal and staff/admin portal. It includes member dashboard, My Coach, workout/session logging, progress tracking, account/subscription/payment/document management, group class schedule, and OpenAI-powered coaching.

## Core product principle
Build this as a premium paid fitness/coaching app experience, not as a basic web portal or admin screen. The app should feel modern, mobile-first, action-oriented and commercially premium.

## UX principles
- Dashboard/Home must answer: "What should the member do today?"
- Dashboard should focus on next workout, start workout, coach action, progress summary, nutrition focus and urgent alerts only.
- Account is for administrative items: member info, subscription, payments, documents, cancellation, password/security, language/settings and logout.
- My Coach is for training, coaching, nutrition and guidance.
- Progress is for results, check-ins, consistency, weight, strength/activity history and motivation.
- Avoid duplicate information across pages.
- Avoid long mobile scroll pages where a focused flow or compact cards would work better.
- Prefer mobile-first UX and clear primary actions.

## Navigation rules
- Main member navigation should be: Dashboard/Home, My Coach, Progress, Account.
- Password/Security must not be a main nav item; it belongs under Account.
- Logout must not be prominent in the main navigation; it belongs under Account.
- On mobile, prefer compact navigation and consider bottom navigation where appropriate.

## i18n rules
The full portal must support exactly these four languages:
- English
- Nederlands
- Papiamentu
- Espanol

All user-facing text must use i18n/translation keys when an i18n structure exists. Do not hardcode UI strings in components.

This applies to member portal, staff/admin portal, dashboard, My Coach, Progress, Account, workout/session flow, coach profile forms, health and safety/pregnancy fields, group class schedule, admin group class management, payment alerts, cancellation policy, documents, validation errors, empty states, loading states, buttons, labels, tabs, notifications, and OpenAI coach UI headings and structured response labels.

Avoid mixed languages on the same page. When a user selects a language, the whole portal should consistently use that language.

## Biological sex and pregnancy rules
The app uses biological sex for training, nutrition and safety logic. Use only:
- Male
- Female

Do not add other gender options. Biological sex is required before generating a full training/nutrition plan.

Pregnancy logic:
- Pregnancy fields are only shown for Female users.
- Pregnancy fields must never appear for Male users.
- OpenAI context must never include pregnancy data for Male users.
- The UI must never show "pregnancy: not pregnant" for Male users.
- If Female, pregnancy status is required: Not pregnant or Pregnant.
- If Female + Pregnant, show and validate pregnancy-specific safety fields.
- Pregnancy guidance is general and must not replace doctor/midwife/healthcare provider advice.

## Safety rules
Health and pregnancy data are sensitive. Keep it privacy-friendly and only use it to adapt training/nutrition guidance safely.

If pregnancy warning symptoms or provider restrictions are present, safety guidance must override fitness goals. Do not generate intense training plans when safety flags require medical clearance.

## Group class schedule rules
- Group classes are part of member training load.
- The group class schedule must come from database/admin data, not from a static PDF.
- Staff/admin must be able to edit, publish and manage the class schedule.
- Members must be able to view classes, add classes to their plan and mark attendance.
- Planned/attended group classes must be included in My Coach, Progress and OpenAI coaching context.
- Completed historical workouts/classes must never be silently modified.
- Future plans may be adjusted when group class changes affect training load, but members should receive a clear notification.

## OpenAI coaching rules
OpenAI coach output must be practical, structured and mobile-friendly. Prefer:
- short summary first
- action points
- clear headings
- bullets
- optional detail sections

Avoid long essay-style answers in the mobile UI. OpenAI context should include relevant training, progress, nutrition, safety, biological sex, pregnancy and group class data where appropriate.

## Workout/session rules
Start Session should feel like an active workout flow, not a long static scroll page. Prefer:
- one active exercise at a time or compact accordions
- set/reps/weight logging
- next exercise flow
- finish workout summary
- completed workout history preserved

## Admin/staff rules
Staff/admin screens should be functional, clear and efficient, but still consistent with Dreamz branding.

Admin must be able to manage:
- group classes
- class types
- weekly schedule
- rooms
- instructors
- schedule changes
- member/account related data where already supported

## Development rules
- Preserve existing functionality unless explicitly changing it.
- Do not break auth, routes, backend calls, database logic, i18n or OpenAI integration.
- Use existing project conventions and styling approach.
- If Tailwind exists, use it consistently. If not, do not add it without checking whether it is safe.
- Run build/lint/typecheck/tests where available after changes.
- Report changed files and any open issues after each task.
