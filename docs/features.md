# Feature roadmap

The first release covers domain-by-domain mail migration from PostfixAdmin or Plesk to an existing Mailcow installation.
Checked items are implemented. Unchecked items are not fully covered; notes distinguish implemented parts from remaining work.
The pre-alpha supports configuration, source inspection, private exports, planning, staging and account verification.
PostfixAdmin isolation, mail/Sieve transfer, activation, quota recalculation and webmail import are available as explicit operations.
Plesk isolation, mail/Sieve transfer, activation, quota recalculation and RoundCube import are also available.
Full handoff and end-to-end validation remain unfinished.

**Preserve** means retain source data and supported behavior within the compatibility matrix.
**Configure** means expose a mapping, policy or operational choice for explicit review.
**Block** means report the affected data and stop the relevant migration before writes until a supported mapping exists.
Blocked data must not be silently omitted. Applicable blockers must be gathered before source isolation or target mutations.

## Configuration generation

Configuration generation is available in the current development version.

- [x] Provide an interactive `mailcow-migrate configure --config FILE` command that generates versioned JSON using
  the existing source/target connection fields, relevant paths, and migration options. Display defaults for review.
- [x] Let the user select PostfixAdmin, Plesk, or both. Prompt only for selected sources and omit unselected sections.
  Require at least one source and construct connections only for configured sources.
- [x] Ask for and validate default quarantine notification frequency/category, initially weekly/Junk folder.
  Provide a shared resolver that preserves normalized source values and falls back per missing/null field to the configuration.
- [x] Validate required values before saving. Write the file atomically with owner-only permissions;
  do not silently overwrite an existing configuration. Store credential-file references rather than secrets in the JSON.
- [x] Make all migration processes load the selected file through one shared loader. Missing or invalid configuration
  must produce an actionable error directing the operator to `configure`; do not silently substitute embedded connections.
  Every operational command passes one configuration-loading gate before dispatch and receives the same validated immutable object.
  Configuration generation remains separate so a new file can be created; missing/invalid files fail with the existing repair guidance.
- [ ] Preserve existing domain-specific settings, policy decisions, and resume identity checks when resolving the configuration.
  Configuration supplies inputs; journals retain operation state. Editing the file must not silently retarget an unfinished batch.
  Saved-plan/configuration checks and account-journal identity are implemented; full workflow resume remains pending.
- [x] Keep configuration generation limited to local configuration output. Server preparation, source isolation,
  account changes, and mail transfer remain separate operations using the saved configuration.

## Inspection and planning

- [x] Provide `inspect DOMAIN --config FILE [--output FILE]` for read-only configured-source inventories, Maildir checks,
  authoritative DNS agreement, current-source matching, and private inspection reports. No target or mail-service changes.
- [x] Provide `export DOMAIN --config FILE` for private domain account/configuration snapshots from
  the currently matching source. Preserve PostfixAdmin SQL/configuration/Sieve evidence and Plesk snapshots/blockers;
  record source identity, file hashes and completion, recheck inventory, and retain interrupted exports under the shared lock.
  Derive the export directory from the workspace, domain and generated export ID, retaining earlier exports without manual paths.
- [x] Provide `plan DOMAIN --config FILE --export DIR --out NEW_DIR` using completed, verified Python exports.
  Preserve source account/alias/password mapping, source blockers, configured quarantine defaults, measured quotas,
  account policy decisions, initial target readiness/conflicts and empty-domain reuse checks. Save a private review and provenance.
  Full migration assessment remains pending and is explicitly distinguished from successful account planning.
- [x] Extend inspection into migration source selection with saved connection settings, cutover-resume detection and
  cross-source operation-journal checks. `select-source` retains the original batch/configuration and checks all configured sources;
  private target-address evidence permits DNS resume only with matching activation identity. `inspect` remains current-source inspection.
- [ ] **Preserve:** Provide guided server preparation, domain migration, source prerequisite checks and SOGo capability checks.
  Add account provisioning, mail transfer, Sieve/SOGo settings, full assessment, administrator and sender-permission import,
  verification, activation and quota recalculation.
- [ ] **Preserve:** Export source account/configuration inventories and record source identity without changing mail service.
  Inspect both schema and effective configuration; keep exports private.
  Domain source exports and separate RoundCube snapshots are implemented; shared-server export and full schema/configuration assessment remain pending.
- [x] **Preserve:** Select PostfixAdmin or Plesk using the existing live configuration, Maildir, and DNS evidence.
  Inspect only configured sources. Preserve saved connection settings, cutover-resume detection, and ambiguity checks.
  Shared selection checks primary and alias-domain routing, recorded activated target addresses and competing batch journals.
  Export uses the same selection and refuses refresh after activation starts or during uncertain account writes.
- [x] **Preserve:** Plan a primary domain with its mail aliases, including domains containing aliases but no mailboxes.
  Reject ambiguous ownership, chained/overlapping domain mappings, and incompatible mailbox paths.
- [x] **Configure:** Measure stored mail and calculate mailbox/domain capacity with explicit quota headroom and minimums.
  Preserve readable message-size evidence; unknown compression without usable metadata blocks accurate sizing.
- [ ] **Preserve:** Produce a reviewable plan and assessment showing proposed changes, conflicts, unsupported data,
  source/target identity, and operator decisions. Preview must not modify either mail service.
  Account, source webmail, shared source settings and sender-policy assessments are implemented.
  Mail storage, rsync capabilities and read-only append previews are implemented.
  Guarded PostfixAdmin mail execution and webmail verification are available separately. Shared-setting application and full cutover/resume remain pending.

## Accounts and messages

- [x] Apply the configured quarantine defaults through the shared resolver in both source planners when source values are unavailable.
  The initial defaults are weekly notifications and the Junk-folder category.
- [x] **Preserve:** Preview and stage inactive domains, mailboxes, display names, aliases, alias domains, and supported catch-alls.
  Unknown-recipient behavior and forwarding-only accounts must have explicit target equivalents.
  Preserve source activity in the plan; verified activation is a separate guarded command.
- [x] Bind account writes to a completed saved plan, record requests before execution and reconcile uncertain outcomes without repeating writes.
  Preserve empty-domain backups, existing-object conflicts and recovery-email readback; verify staged account inventories explicitly.
- [x] Provide guarded PostfixAdmin account activation after final mail/Sieve, sender and prefilter checks.
  Require explicit readiness/stopped-writer confirmation, pin ordered target requests, read back each edit and reconcile uncertain
  outcomes without repeating writes. Preserve planned inactive accounts and restore the planned domain state. Verify partial
  activation against receipts while retaining source/target isolation; webmail completion and release remain separate.
- [x] **Preserve:** Transfer accepted mailbox password hashes without requiring user resets. The PostfixAdmin baseline accepts
  MD5-CRYPT only. Plesk accepts MD5-CRYPT, SHA256-CRYPT, SHA512-CRYPT, and BLF-CRYPT; available plaintext is converted to SSHA256.
  Verify target scheme support and block malformed, truncated, or unmapped credentials.
  Share format validation across planning and staging, check live Dovecot scheme availability and read back stored hashes before
  staging completion and guarded activation. Explicit `verify --passwords` compares the original hashes; routine verification
  preserves later password changes. Native reconciliation remains available; IMAP/SMTP login checks belong to operator handoff.
- [x] **Configure:** Preserve mailbox forwarding destinations and keep-copy behavior. Account for personal-filter ordering;
  the current Plesk path blocks forwarding combined with personal Sieve until an ordering adapter exists.
  PostfixAdmin uses reviewed native settings/prefilters. Plesk copies its generated redirect/copy/stop script from verified export evidence.
- [x] Inspect the target mail volume and vmail identity, validate mailbox paths, and probe sender/receiver transfer capabilities separately.
  Compare existing Maildirs with checksum dry runs, retaining file-type/content and alternate-message-path blockers.
  Keep source checks pending with fixed quotas or source mapping blockers, and retain staging/isolation/journal guards before actual copying.
- [x] **Preserve:** Copy Maildir messages and supported metadata with append-only rsync, preserving existing target files.
  Conflicting content, unsafe links, source changes, and unaccounted writers must stop copying or activation.
  Include final verification and target quota recalculation.
  Both sources use shared transfer/volume receipts, final-pass checks, guarded activation and quota recalculation.
  Plesk additionally verifies its complete account/filter snapshot under native login/submission/delivery isolation.
- [x] Provide guarded PostfixAdmin mail preview/apply with explicit stopped-writer confirmation and live isolation/inventory checks.
  Check all source Maildirs before copying, reuse checksum conflict checks, preserve existing files and show rsync counts/progress.
  Bind private transfer receipts to source/target identity, clear prior final-pass eligibility before each applied attempt,
  and record a final pass only after copying and renewed checks. Sieve, activation and quota recalculation are separate guarded steps.
- [x] Recalculate quota for planned active mailboxes after activation on the original copied mail volume.
  Preserve disabled-user/domain behavior, native diagnostics and command completion; journal pending users and repeat interrupted
  recalculations under fresh isolation checks. Bind completion to transfer evidence and require it before webmail import or target release.
- [x] **Configure:** Import valid recovery email addresses and map password-expiration policy.
  Recovery phone/SMS data and unmapped password-expiration behavior are blockers.
  Preserve recovery-email API readback and enforce the reviewed disabled-expiration decision during staging and activation.
  Pin the saved policy in new planning manifests and before account writes; changed decisions block resumption.
  Dated/recurring expiration remains blocked rather than being replaced with forced password updates.
- [x] **Configure:** Map PostfixAdmin domain administrators, privileges, usernames, ownership, and new passwords explicitly.
  Review global administrators separately; Plesk panel users are outside this mail-domain migration.
  Preview the saved proposal, require explicit rename/merge ownership decisions, preserve existing memberships/security,
  generate private new credentials and reconcile uncertain writes before retrying. Read back imported grants; webmail import
  requires completed administrator receipts. Global administrator application remains part of the separate shared-server work.
- [ ] **Configure:** Preserve supported send-as grants and BCC rules without widening existing target permissions or scope.
- [x] Apply explicit sender-permission mappings to staged inactive mailboxes and verify them through the target API.
  Recognize existing direct/wildcard/alias coverage, retain old ACL entries and password-change/two-factor requirements,
  and journal uncertain edits for reconciliation without blindly resending them. Read-only checks remain available after activation.
  New grants require explicit batch addresses; SMTP enforcement and complete sender-policy verification remain separate.

## Filters and webmail

- [x] **Preserve:** Inventory and copy supported personal/default Sieve, compile it on the target, and preserve activation state.
  Default-filter selection and replacing existing target scripts require explicit decisions and backups.
  Plesk scripts and forwarding come from the private JSON export, bypass editable-filter conversion, and use the same installation/readback guards.
- [x] Provide guarded PostfixAdmin Sieve preview/apply using the planning export and shared transfer isolation/journal checks.
  Preserve personal/default selection, inactive/unsupported-script blockers, full-batch inspection, regular-file/ancestor checks,
  private conflict backups, vmail ownership and native compilation. Clear earlier Sieve completion before each applied attempt.
  Editable SOGo integration is available for PostfixAdmin; Plesk installation is supported. End-to-end validation remains pending.
- [x] **Preserve:** Convert the supported PostfixAdmin Sieve subset into planned editable SOGo filters.
  Save personal/default scripts, converted rules, blockers, native preferences and staging prefilters in a private webmail plan.
  Keep Plesk personal Sieve and forwarding scripts on their copy/compile path without imposing editable-filter conversion.
- [x] **Preserve:** Import and verify supported PostfixAdmin editable SOGo filters against target preferences and active scripts.
  The internal backend implements capability/conflict checks, native preference restoration and stored/active script readback.
  The webmail command enforces transfer/activation/isolation prerequisites before this stage.
- [x] Implement the native PostfixAdmin editable-filter import backend with private backups and per-user pending receipts.
  Require matching active/stored scripts, preserve unrelated defaults/settings, block enabled native vacation/forward/notification
  conflicts, and verify converted preferences before activating the stored script. Reconcile interrupted work through fresh reads;
  retain later user edits after completion. Full webmail completion and native vacation/forwarding handoff remain separate.
- [x] **Configure:** Convert PostfixAdmin vacations and forwarding into supported SOGo/Sieve settings, preserving enabled state,
  date windows, interval semantics, and local-copy/filter ordering. Timezone and zero-interval policies must be explicit.
  Source mapping and assessment are implemented, including whole-day timezone checks for inactive and expired records.
  Target timezone agreement, import and verification are connected through the PostfixAdmin webmail command.
  Prior autoresponder reply history is not transferred.
- [x] Implement the native SOGo vacation/forwarding backend using the reviewed webmail proposal and completed filter import.
  Check both containers' vacation timezones, existing preferences and active/stored scripts; retain prefilter forwarding when
  personal rules control the local copy. Preserve unrelated settings, verify generated vacation date guards and stop on uncertain
  writes with private backups. Native completion remains separate from full webmail completion and target release.
- [x] Stage reviewed PostfixAdmin forwarding/vacation prefilters inactive before activation.
  Reproduce the saved proposal at its original planning time, retain exact matching inactive filters, block conflicting or active
  filters, and record API intent before writes. Reconcile uncertain results without repeating requests and verify inactive readback.
  Preserve Sieve evidence fingerprints and forwarding identity; activation/handoff backends are separate from workflow integration.
- [x] Implement guarded activation and verification of staged forwarding/vacation prefilters internally.
  Require final mail/Sieve receipts and matching source fingerprints, retain exact target IDs and journal each activity edit.
  Reconcile only confirmed outcomes, reject conflicts and preserve completed native handoffs without enabling duplicate rules.
  The account-activation command supplies the shared lock and live source/target isolation checks.
- [x] **Preserve:** Export RoundCube contacts, groups, identities, signatures, supported preferences, and folder display state
  for either source adapter. Import and verify SOGo data before restoring access; reject conflicting target settings.
  Source snapshots, selected/ignored-profile inventories, file fingerprints and supported SOGo proposals are available.
  Both source paths enforce isolation/activation/quota prerequisites and native readback. Plesk retains its copied personal Sieve;
  PostfixAdmin additionally imports editable filters and native settings. Completion requires exactly the stages for that source.
- [x] Map supported RoundCube vCards, collected recipients, identities, preferences and source folder display state into reviewable private proposals.
  Preserve stable record IDs, existing UIDs, defaults, unsupported-data blockers and source snapshot/folder drift checks.
  Record review of an unchanged proposal and explicit deferral of supported receipt/reply-folder preferences before source isolation.
  Target preparation remains separate; webmail import reuses the approved proposal and native conflict/folder checks.
- [x] **Configure:** Review SOGo settings needed for identities, forwarding, filters, and vacations before enabling them.
  Explain any effect on other domains. Unsupported preferences need an explicit, recorded decision.
  Target configuration preview/application for custom identity support is implemented, with explicit shared-restart scope,
  private backups, pending-operation checks and effective-setting readback. Webmail import enforces the required native-filter capabilities.
- [x] Inspect existing SOGo preferences and destination address books through native backups without changing user data.
  Preserve exact conflict/ownership checks and the first private backup; missing user sources and failed lookups block inspection.
  Native inspection does not authorize writes, refresh sources, clear cached user records or mark import complete.
- [x] Explicitly prepare SOGo user lookups before native checks, restarting only for missing effective user sources.
  Explain the shared webmail/DAV interruption and recheck settings after startup. Preserve the targeted cached-null deletion rule,
  private attempt records and fresh native verification; keep activation and user-data import separate.
- [x] Recheck source snapshots/folder trees and assess target folder display state during native user inspection.
  Require every source folder on the target, preserve SOGo identifier escaping and the collapsed/expanded mapping,
  and block conflicting primary-account selections while retaining unrelated Mail settings and other account identifiers.
  Save proposed values privately; the internal write/readback layer is separate from inspection and full import completion.
- [x] Implement the native SOGo import backend for approved preferences, contacts and folder display state.
  Preserve all-user conflict checks, private first backups, pending-write receipts, additive 50-contact chunks and native readback.
  Reconcile interrupted writes and retain later edits after native completion. The webmail command supplies source/readiness checks.
- [x] Connect the PostfixAdmin webmail stages in order: RoundCube data, editable filters, then native vacation/forwarding settings.
  Require stopped writers, completed transfers/activation, reviewed proposals and live source/target holds. Retain partial-stage
  recovery and approved deferrals; bind full completion to input fingerprints and all three backend receipts. Target release
  rejects missing, partial, foreign or changed receipts and remains a separate explicit action.
- [x] **Preserve:** Identify and revoke database-backed RoundCube sessions after verified PostfixAdmin login and delivery isolation.
  Preserve exact session ownership/value checks, draft confirmation, zero-session readback and unchanged user-data snapshots.
  Retain separate receipts and evidence for both sources. Activated-batch repair requires held target access, saved drafts,
  stopped webmail writers and verified source controls before session revocation; it does not recopy accounts or messages.
- [x] Support separately journaled target isolation for missing webmail imports on an already activated batch.
  Require completed prior target restoration for the same plan and target; preserve its journal and save current access/job settings.
  Reuse owned guards and the existing pause/restore checks. Select the repair journal for import readiness and restoration,
  retaining full webmail/quota completion gates and refusing to restart completed imports or released isolation.

## Server preparation and handoff

- [x] Assess shared PostfixAdmin settings and per-service sender restrictions from completed source exports without applying changes.
  Classify complete map inventories, supported routes, relayhost, sender rejection, IP-whitelist adaptations and source blockers.
  Preserve domain relationships and shared dependencies; save source values and per-service findings privately.
  Plesk domain planning does not inherit the PostfixAdmin shared-setting import or fabricate missing sender evidence.
- [ ] **Configure:** Review shared PostfixAdmin server settings before per-domain changes: supported transport routes,
  outgoing relay credentials, sender restrictions, individual-IP whitelist adaptations, and server administrators.
  Preserve explicit exclusions and explain semantic differences. Plesk mail-domain support does not imply importing all panel policy.
  Source assessment is available; guided selections/exclusions, credentials, privilege decisions, application and target verification remain pending.
- [ ] **Preserve:** Isolate source logins, submission, and delivery using the selected adapter; disconnect affected sessions
  and revoke webmail access while leaving unrelated domains operational.
  Plesk supports native preview/freeze/check/pre-activation release, Dovecot denial, SMTP/deferred delivery, worker draining,
  writer checks and RoundCube revocation. Combined PostfixAdmin submission checks remain pending.
- [x] Preview, freeze, check and release PostfixAdmin batch logins using the supported SQL passdb and exact query exclusions.
  Preserve other batches' exclusions, source-side full configuration backups, cache flushing and session checks.
  Retain a source/plan-bound operation journal and block changes after target activation starts or during uncertain target writes.
  Delivery prerequisites are checked before freeze; release remains available when those prerequisites fail.
- [x] Preview, pause and check PostfixAdmin delivery for one primary domain using persistent retry routes and recipient identity maps.
  Preserve other domains and original BCC maps, retain filtered copies/backups, drain workers and verify queue envelopes and detected writers.
  Record queue-history evidence and block ambiguous prior expansion, original-recipient history, BCC copies and administrative holds.
  Keep the global original-recipient setting and queue scheduling unchanged; do not delete or globally requeue messages.
  RoundCube session revocation and its own verification run after delivery holds; full source submission checks,
  guarded handoff and the separate Plesk adapter remain pending.
- [x] **Preserve:** Isolate target access, delivery, and relevant background writers during staging and verification.
  Do not silently stop an unrelated or shared job whose ownership is unknown.
  Preview and explicitly pause staged batches; retain original settings, configuration backups and interrupted route updates.
  Check protocol controls, sessions, SOGo guards, delivery holds, sync jobs and detected scheduled/open-file writers.
  Restoration requires completed activation, quota recalculation and the full webmail receipt with matching native stage receipts.
  Read-only target isolation checks remain available during and after account activation.
- [ ] **Preserve:** Guide DNS/certificate handoff, inspect both queues, verify MX/address records and SPF/DKIM,
  and require operator delivery/client checks including received-message authentication results.
- [ ] **Configure:** Offer confirmed certificate setup, DKIM generation, relay assignment, and per-domain source forwarding.
  DNS edits remain operator-managed. Preserve recipient validation, alias domains, and catch-all policy in forwarding.
- [ ] **Preserve:** Mark a batch complete only after required imports, activation, verification, and delivery confirmation succeed.

## Recovery and configuration

- [ ] **Preserve:** Use a shared lock, private exports/backups, versioned journals, atomic state writes, and drift detection.
  Record mutation intent before execution and reconcile uncertain results before retrying.
- [ ] **Preserve:** Resume without duplicate accounts, replacing mail, or overwriting later user edits.
  Retain recovery evidence after interruptions and failures.
  Account staging resumes its own receipts and pending requests; target isolation retains resumable pause/release state.
  PostfixAdmin login controls retain a resumable operation journal and support release before activation.
  Source delivery retains operation stages, source configuration backups and recipient-isolation evidence; delivery release remains pending.
  Mail copying retains pending/final-pass receipts and resumes through fresh checks and append-only copying.
  Sieve installation shares transfer identity/isolation checks and retains pending stages, export fingerprints and conflict backups.
  Inactive prefilter staging retains source/policy identity and reconciles uncertain API outcomes against live target data.
  Sender-permission staging preserves baseline ACLs and security attributes, with request reconciliation and live readback.
  Editable-filter imports retain source identity, native backups and pending preference receipts; unrelated drift blocks resumption.
  Native vacation/forwarding imports retain per-user snapshots and stop on pending writes until explicit reconciliation.
  Prefilter activation shares staging identity/receipts and verifies uncertain activity edits without repeating them blindly.
  Account activation retains pinned requests, ordered receipts and verification of partially restored activity.
  Full source isolation and other workflow journals remain pending.
- [x] **Preserve:** Allow source release only within the supported pre-activation recovery boundary.
  Activation/forwarding changes require their own reconciliation; do not claim a general rollback after cutover.
  Refuse activation intent, uncertain requests, missing account evidence after target isolation, and recorded forwarding/completion.
  PostfixAdmin restores only its login exclusion and retains delivery holds; Plesk restores its journaled access/delivery controls.
- [ ] **Configure:** Preserve the existing connection settings, CLI options, policy files, prompts, defaults, and validations.
  Further generalization beyond the configuration wizard and independent source selection is future work.
- [x] **Preserve:** Detect unfinished batches and coordinate locking across preparation and migration operations.
  Validate saved settings, state versions and fingerprints before resuming.
  Discover the domain's workspace planning tree and register all newly planned or explicitly loaded batch paths in a private catalog.
  Show incomplete planning, retain released-operation ownership and reject competing journals before new planning or saved-plan operations.
  Older batches outside the discovery tree require an explicit original plan. Unsupported formats stop without conversion;
  any future conversion must preserve operation ownership and recovery evidence before resuming mutations.
- [ ] **Preserve:** Keep terminal prompts/progress useful, errors visible, and logs free of passwords, API keys, and message content.
  Package every required runtime resource inside this project.

## Explicit blockers and exclusions

| Classification | Data or configuration | Initial handling |
| --- | --- | --- |
| Block | Plesk autoresponders and Mailman lists | Inventory and require a separate supported migration. |
| Block | Fetchmail jobs, including disabled jobs | Require an equivalent target mapping; do not substitute IMAP sync silently. |
| Block | Inactive/additional Sieve, external includes, or unsupported syntax where conversion is required | Retain inventory and require mapping. |
| Block | Unknown password schemes, phone recovery, unmapped expiry | Report affected accounts before staging. |
| Block | Unknown RoundCube tables/plugin data, external address books, unsupported sessions | Require an exporter/storage adapter. |
| Block | Custom routing, filters, canonical rewriting, or ambiguous queue history | Require a reviewed adapter for that source. |
| Block | Backup-MX domains or unsupported alias/recipient policies | Require an explicit delivery mapping. |
| Block | Source/target drift, uncertain mutations, or conflicting existing target data | Reconcile evidence before continuing. |
| Exclude | Websites, Plesk panel accounts, Horde and other webmail providers | Outside the initial source adapters. |
| Exclude | Calendars/tasks from external systems and complete spam/antivirus policy replication | Separate migration scope. |
| Exclude | Automatic DNS edits, multi-domain parallel execution, and general post-cutover rollback | Operator workflow or later scope. |

Configuration differences must be assessed per adapter. For example, the existing Plesk handoff preserves a supported
recipient-canonical map chain, whereas unsupported canonical rewriting remains a blocker elsewhere.
Declared exclusions must appear in the assessment whenever relevant data is discovered.

New exporters, additional supported formats/releases, and configuration generalization beyond the required generation
and source-selection steps are deferred. Unsupported data remains blocked until a reviewed mapping is implemented.
