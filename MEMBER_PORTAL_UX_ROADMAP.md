# Dreamz Member Portal UX roadmap

Status: canonical working plan for the URL-based member portal
Baseline: production commit `84734ce7c53871e3a1db4d5544e4305614078a34`
Integration branch: `codex/member-portal-ux-integration`

## Product boundary

The member product is a responsive web portal served from
`https://dreamzfitness.app`. Phone usage is a primary use case, but native
Android and iOS applications are not part of the current product scope.

Keep the following out of member-portal UX releases unless this decision is
explicitly revisited:

- app-store distribution;
- native Android/iOS shells and release signing;
- Android App Links and `assetlinks.json`;
- a separate `/app` installation route;
- re-enabling a PWA installation prompt;
- download/install messaging that presents the portal as a store app.

The existing browser manifest, service worker and offline notice may remain
available as web infrastructure. They must not bypass the current
`display: browser` and install-prompt pause.

## Consolidated sources

This roadmap combines:

- the authenticated production audit of the current member portal;
- the earlier "Analyseer Gym Assistant MemberConnect" task;
- the existing committed mobile, information-architecture, achievement and
  performance refinements;
- the current decision to optimise the URL portal before considering native
  applications.

Use this integration branch as the single implementation lane. The dirty
historical worktree is reference material only and must not be deployed or used
as a parallel writer.

## Phase 1: navigation and dashboard focus

Implemented in this branch:

- exactly four main member destinations: Dashboard, My Coach, Progress and
  Account;
- Nutrition, Group Classes and Equipment remain available inside the coaching
  journey, with My Coach highlighted on those pages;
- "Today at Dreamz" appears directly after the primary workout action and
  before Nutrition;
- the class schedule has a clearer primary call to action;
- "Today is ready" is replaced by the truthful "Next workout ready" label;
- the four-item navigation is a real bottom bar on mobile and remains in the
  header on tablet and desktop.

Acceptance checks:

- no member functionality or routes are removed;
- account invoice/download routes and account notifications remain intact;
- group-class data still comes from the database;
- all four supported languages retain translation-key parity;
- phone, tablet and desktop layouts have no navigation overlap.

## Phase 2: reliable active-workout flow

Implemented in this branch as the durable foundation before adding more
gamification:

- a server-side workout session is created when the member starts;
- one active exercise is shown at a time, with an accessible compact overview;
- individual sets record reps or time, weight and optional effort/RPE;
- progress autosaves and resumes after refresh, reconnect or device change;
- start, save and finish requests are idempotent;
- completed workout history remains immutable;
- finishing produces a concise summary and explicit next action.

This phase is the data foundation for trustworthy personal records, volume
charts, streaks and level progression.

## Phase 3: useful progress visualisations and gamification

After Phase 2 data is reliable:

- weekly consistency and workout/class heatmap;
- weight trend with an honest empty/insufficient-data state;
- strength or training-volume trend;
- personal records derived from set-level history;
- streaks, milestones and XP based on completed actions;
- a compact "what changed" summary and one recommended next action.

Avoid decorative scores that cannot be explained from member data. Every badge,
level and comparison must show what unlocked it.

## Phase 4: coach clarity and schedule awareness

- render coach responses as safe, structured rich text instead of raw Markdown;
- use a short summary, action points and optional detail;
- distinguish today's workout from the next workout;
- include planned and attended group classes in training-load guidance;
- keep safety, biological-sex and pregnancy rules authoritative;
- prevent long essay-style responses on mobile.

## Phase 5: measurement, accessibility and performance

Portal-usage measurement is deliberately not included in Phase 1. The earlier
implementation wrote identifiable per-member activity to a new production
table, so it needs a separate decision covering:

- purpose and legal/privacy basis;
- aggregate versus per-member reporting;
- retention and deletion;
- mobile-browser versus desktop granularity;
- staging and additive schema rollout;
- query cost and the meaning of historical activity.

If approved later, store no IP address or raw user-agent data. Native-app and
PWA entry-source categories remain out of scope.

Continue to validate:

- keyboard focus and screen-reader labels;
- reduced-motion behaviour;
- 390 px, 768 px and 1440 px layouts;
- EN, NL, PAP and ES copy;
- page weight, loading states and mobile interaction latency.
