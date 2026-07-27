# Existing-member reverification → Member Portal

## Release status

- Prepared: `2026-07-26T17:07:26-04:00`
- Status: local release candidate; **not deployed**
- Portal base commit: `84734ce7c53871e3a1db4d5544e4305614078a34`
- Local release branch: `codex/existing-member-portal-handoff`
- Plan-binding hardening commit: `b71f45a8589f0442ef24aaab0ea1a8d930e827d5`
- Production mail sent: no
- Production data changed: no
- Gym Assistant data changed: no

## Scope

This release adds the server-to-server handoff from a completed, signed and
email-verified existing-member reverification in the Signup app to the existing
Member Portal account model.

The handoff never creates a second member record. It targets the Portal member
whose `member_id` is the existing Gym Assistant member number.

The canonical request consists of exactly these six scalar strings:

1. `request_type`
2. `reference`
3. `member_number`
4. `expected_email`
5. `language`
6. `signup_plan`

The Signup app recursively sorts and compactly serializes the canonical object,
then sends its lowercase SHA-256 digest both as `idempotency_key` and as the
`Idempotency-Key` header. The Portal recalculates and verifies the same digest
before storing or reconciling the request.

## Fail-closed pilot controls

All existing-member reverification invitations are denied by default:

```dotenv
PORTAL_EXISTING_MEMBER_REVERIFICATION_ENABLED=false
PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_MEMBER_IDS=
PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_EMAILS=
```

The feature flag and **both** exact allowlists must match before the Portal can
send an activation/login email. Empty or one-sided allowlists deny every pilot
request.

Safe nonterminal statuses are:

- `waiting_for_feature_enablement`
- `waiting_for_pilot_allowlist`
- `waiting_for_gym_assistant_email`

An absent or changed Gym Assistant email therefore waits for a later sync. A
duplicate email, invalid request, ineligible membership or integrity mismatch
fails closed to manual review.

For existing-member reverification, the current Gym Assistant plan is also
mapped back to the canonical Signup plan (`month`, `six`, `twelve` or
`under18`). A different current plan fails closed to manual review before any
Portal email can be sent.

## User handoff

After the signed reverification is durably saved, the Signup completion screen
offers a localized `Member Portal` button pointing only to the normal `/login`
page. No bearer token, password or automatic login is passed through the
browser.

The Portal retains its existing login behavior:

- password login for an existing configured account;
- email-code login when no password exists;
- mandatory password setup after the first email-code login.

## Verification evidence

### Signup app

- Full `npm test`: all 17 suites passed.
- `npm run build`: passed.
- `node scripts/test-existing-member-reverification.mjs`: passed.
- `node scripts/test-existing-member-reverification-email-gate.mjs`: passed.
- `node scripts/test-portal-invitation.mjs`: passed.
- `node scripts/test-submission-idempotency.mjs`: passed.
- Cross-language canonical SHA-256 fixture:
  `e45be34864baca92e63f9f4999cb832c5d286e147c6a23ffc324d163b5a938b2`
  in both Node.js and Python.

### Member Portal

- Invitation module: 23 tests passed.
- Real login-path checks: 3 tests passed.
- Python compile check: passed.
- Full regression suite: `Ran 429 tests in 1057.340s` —
  `OK (skipped=1)`.

Three failures found during the first full run were reproduced unchanged on the
untouched base commit. Their test fixtures were repaired without changing
production code:

- two document-serving tests now create their own temporary PDF;
- the pregnancy class-safety test now creates its own bookable BODYCOMBAT
  occurrence instead of relying on the current seeded weekly timetable.

## Reviewed source hashes

### Portal release

| File | SHA-256 |
| --- | --- |
| `.env.example` | `CAE32227E4F9D4EC0A595BE2DFA2385BF79AE556E97506ADE1DBB68A3B3AB6BB` |
| `DEPLOYMENT.md` | `256ACA30D91DA39F335AD769E080D7750EA7FDADC65A3D4660D070033C8488B1` |
| `dreamz_portal.py` | `C92580342745CED900162F006C113C1537440099C363E994EC9896DA8A564D19` |
| `tests/test_portal_invitations.py` | `9EA838C1B9573CAB64B92AB7AA11E144FCE619F87239F00A6404FBCD691D6CFA` |
| `tests/test_portal_routes.py` | `5F3825D61C908943C00B978990F56DDC803E2A8AFFC4ECA4162C6B43CBA04287` |

### Signup working snapshot

The Signup repository contains earlier in-progress Dreamz work and is therefore
not represented as a clean base commit in this Portal release. These hashes bind
the exact locally tested handoff snapshot:

| File | SHA-256 |
| --- | --- |
| `server.mjs` | `1537D39B4D6A955D8DDB5410954F51DD60942074232E4E2C3E43539825EF24C3` |
| `app.js` | `FEB0FDE9872B5A090DB073C31B4648935ADBFC4E08D5036C24F4AB86F77202D7` |
| `scripts/test-existing-member-reverification.mjs` | `BD222EAF8BC6171D244152B3B1A11CC78DA94213C556EF36C2B0C604934FE34A` |
| `scripts/test-existing-member-reverification-email-gate.mjs` | `A3B9DD1EC64895856ED72E6FFE0DF57AAEE9E2E601E2D41A3C3C6165916014B0` |
| `scripts/test-portal-invitation.mjs` | `89C8863B5D9D84E5CA8A2D2875320B62B9AC7065A4792534F0529AE7062EB801` |
| `scripts/test-submission-idempotency.mjs` | `76099D64FAF05F95E2797D8225FAA0FD24881290DA3E8159A8CB2A8C04EB3E3D` |
| `package.json` | `E34DE30EFC9745B1B8E0E1C0537BE1E8D7071F4D7A81F9CDF14AD70F70455603` |
| `package-lock.json` | `FF0F5ACC593947E88EC887D85FE40AEAA08AD56D1EC1837B84B0C94B5637D48F` |

## Controlled rollout order

1. Record the currently deployed Portal and Signup revisions and configuration.
2. Take and verify the required production database/configuration backup.
3. Deploy the Portal release with the feature flag off and both allowlists empty.
4. Run health, login and legacy new-member invitation smoke tests.
5. Deploy the exact reviewed Signup snapshot and verify that an existing-member
   request remains in `waiting_for_feature_enablement`.
6. While the feature flag remains off, add exactly one reviewed pilot member
   number and its exact verified email to the two Portal allowlists.
7. Enable the feature flag during the reviewed pilot window.
8. Run only the Ron pilot and verify:
   - no duplicate Portal member;
   - exact Gym Assistant member-number binding;
   - exactly one localized activation/login email;
   - normal email-code/password login;
   - no Gym Assistant write from this handoff.
9. Disable the feature flag again until the pilot evidence is reviewed.

## Rollback

Immediate kill switch:

```dotenv
PORTAL_EXISTING_MEMBER_REVERIFICATION_ENABLED=false
```

Then:

1. restore the previous Portal deployment or redeploy base commit
   `84734ce7c53871e3a1db4d5544e4305614078a34`;
2. restore the previously recorded Signup deployment if its release introduced
   any smoke-test regression;
3. keep invitation records for audit; do not delete or replay them manually;
4. verify legacy login, sync and new-member invitation behavior.

## Remaining live gates

No production pilot may start until the Dreamz remote-operations runbook permits
the change and the following are known and reviewed:

- currently deployed immutable revisions;
- verified backup and rollback target;
- production integration token alignment;
- exact Ron member number and verified Gym Assistant email;
- health/login/new-member smoke-test results with the feature still disabled.
