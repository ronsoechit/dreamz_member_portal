# Existing-member reverification to Member Portal

## Release status

- Prepared: 2026-07-31
- Status: clean local release candidate; **not deployed**
- Exact live Portal base: `790aee4a7b216a0268266817af1f390331fe316a`
- Exact live Portal base tree: `80754d8f8b9651f0953ff3341dd4be3917911fd1`
- Candidate branch: `codex/existing-member-pilot-livebase-20260731`
- Production mail sent: no
- Production data changed: no
- Gym Assistant or frontdesk touched: no

The candidate is reconciled directly on the current live Portal revision. It
does not copy the older dirty development file and therefore retains the live
KMAR, login, sync and other later Portal behavior.

## Protected handoff

The Signup app may request a Portal invitation only after the signed and
email-verified existing-member update has completed and the same Gym Assistant
member has passed read-back verification.

The canonical request binds these six scalar strings:

1. `request_type` (`existing_member_reverification`)
2. `reference`
3. `member_number`
4. `expected_email`
5. `language`
6. `signup_plan`

Signup sends the lowercase SHA-256 digest of the sorted compact JSON object as
both `idempotency_key` and the `Idempotency-Key` header. Portal recalculates the
digest and echoes `request_type`, `member_number` and `idempotency_key` in its
authenticated response so Signup can reject a response for another request.

Portal targets only the already-synced member with the exact Gym Assistant
member number. It never creates a second Portal member. A changed or missing
email waits for a later Gym Assistant sync. A duplicate email, ineligible plan,
plan mismatch or integrity mismatch fails closed.

## Fail-closed pilot controls

The feature is disabled by default:

```dotenv
PORTAL_EXISTING_MEMBER_REVERIFICATION_ENABLED=false
PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_MEMBER_IDS=
PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_EMAILS=
```

Enabling the flag is insufficient. Each allowlist must contain exactly one
non-empty value, and both values must match the same request. Empty, one-sided,
duplicate or multi-value configuration remains in
`waiting_for_pilot_allowlist`.

The existing-member invitation is SMTP-only. If
`EMAIL_DELIVERY_MODE` is not exactly `smtp`, it remains in
`waiting_for_smtp_configuration`, creates no email-log row and does not claim a
send attempt. After a send claim, only the exact delivery result `sent` may
become terminal `sent`; failures or ambiguous results require manual review.

## Compatibility retained

- Current and legacy normalized KMAR Gym Assistant plan names remain mapped to
  `kmar_2026` exactly as in the live baseline.
- Legacy/new-member Portal invitations keep their existing log-mode behavior;
  SMTP-only enforcement applies only to existing-member reverification.
- Normal Portal password and email-code login remain unchanged.
- No database schema change is introduced by this hardening delta.

## Verification evidence

- Existing-member/Portal invitation tests: 32 passed.
- Portal login, route and sync regression subset: 253 passed.
- Full Portal regression suite: 682 passed, 1 skipped, in 1326.409 seconds.
- The focused final hardening tests were rerun after the review fixes.
- Python compile, feature-off startup and diff checks: passed.
- Independent code/security review: no remaining blocker.

## Controlled rollout order

1. Commit and hash the exact reviewed Portal and Signup candidates.
2. Back up the live Portal database and Signup `/data` volume; record current
   configuration and rollback revisions.
3. Deploy Portal first with the feature flag off and both allowlists empty.
4. Run health, normal login, member sync, normal invitation and KMAR smoke tests.
5. Deploy Signup with its feature flag off and both allowlists empty.
6. Run normal signup, Delfins, KMAR, pending-contract and admin smoke tests.
7. Configure exactly the reviewed `<pilot-member-number>` and its separately
   verified Gym Assistant email in both services, then enable only that one
   pilot during a reviewed window.
8. Complete the update, signed-form review, update-only Gym Assistant write,
   exact read-back, Portal invitation and normal login test.
9. Disable the pilot again and review the audit evidence before adding anyone
   else.

## Rollback

- Immediate kill switch:
  `PORTAL_EXISTING_MEMBER_REVERIFICATION_ENABLED=false` with both allowlists
  empty.
- Restore the Portal deployment for commit
  `790aee4a7b216a0268266817af1f390331fe316a` if a Portal smoke test fails.
- Restore the Signup deployment for commit
  `227d5039a77da5df8311e8706d6307ebeea3528a` if a Signup smoke test fails.
- Keep invitation and audit records; do not delete or replay them manually.

No production deployment or pilot is authorized by this document alone.
