"""The held-out tasks, written from three real Grafana changes.

Each entry names a merged change that landed after the cutoff. The
brief is the problem as a requester would have put it, written by
reading the change's own motivation with the solution taken out; the
diff is never shown to the agent. The key is what the project in fact
did, quoted from the commit, with the person who in fact did it. It
is what happened, not the only defensible answer: `answer` is what the
simulated owner replies, `agree` is what they read to decide whether
Raven's own resolved answer already says it, and `adherence` is what
has to be true of the diff.

The briefs, the decisions and the answers were written before any run
and have not been touched since, with one exception noted below. The
`adherence` checks have been corrected twice, both times because they
were measuring the wrong thing rather than because of what a run scored:

- The first version graded the agent's closing summary, which let a run
  that described the right change but did not make it read as a pass.
  They read the diff now.
- The golden migration-ids file was required to be edited, until
  `migration_ids_golden_test.go` in the repository turned out to say in
  as many words that a coding agent must not regenerate it and should
  stop and ask instead. Requiring it would have been requiring the agent
  to disobey the repository. That is the one decision whose wording
  changed: it now asks who regenerates the file, which is what the
  repository itself makes a decision.

See README.md.
"""

CUTOFF = "2026-09-01"
REPO = "grafana/grafana"
SCOPE = ["pkg/services", "pkg/api"]

# CODEOWNERS names teams, not people. Expanding a team into the people
# on it is the operator's setup step in any pilot; here it is done from
# authorship before the cutoff only, so nothing from the held-out window
# reaches the map. See README.md.
TEAM_PREFIXES = {
    "@grafana/grafana-backend-services-squad": ["pkg/services/featuremgmt"],
    "@grafana/alerting-squad": ["pkg/services/ngalert", "pkg/services/sqlstore/migrations/ualert"],
    "@grafana/identity-squad": ["pkg/services/authn", "pkg/services/oauthtoken"],
    "@grafana/grafana-search-and-storage": ["pkg/services/sqlstore/migrations"],
}

TASKS = [
    {
        "id": "toggle-handling",
        "commit": "a89e40005f118d6288611b33f6073f7f316c5f7c",
        "title": "Report or block reads of the legacy frontend feature-toggle map",
        "brief": (
            "Plugins and third-party frontend code still read the legacy `config.featureToggles` map "
            "from grafana-runtime directly instead of going through the OpenFeature client. We want to "
            "see who is still doing it, and eventually to stop it.\n\n"
            "Add the server-side half: register a feature toggle in pkg/services/featuremgmt/registry.go "
            "that controls what the frontend does when something reads the legacy map, and carry its "
            "value into the frontend bootstrap data (pkg/api/dtos/index.go and the frontend index "
            "service) so the runtime can act on it.\n\n"
            "There is already a toggle for the equivalent problem on the API side, "
            "`grafana.frontendLegacyAPIHandling`; look at how it is declared."
        ),
        "paths": ["pkg/services/featuremgmt/registry.go", "pkg/api/dtos/index.go",
                  "pkg/services/frontend/index.go"],
        "team": "@grafana/grafana-backend-services-squad",
        "owner": "Engineer 419848",
        "decisions": [
            {
                "match": ["default", "value", "state", "off", "log", "block", "three", "boolean"],
                "question": "What values should the new toggle take, and what should it default to?",
                "answer": ("Three states, not a boolean: \"off\", \"log\" (report each toggle once) and "
                           "\"block\" (report, and resolve every toggle to undefined). It ships defaulting "
                           "to \"off\", so nothing changes for anyone until an operator turns it on."),
                "rationale": "We need the reporting stage before we can break anybody; off is the only safe default.",
            },
            {
                "match": ["frontendlegacyapihandling", "existing toggle", "api handling", "boolean", "consistent"],
                "question": "Should the existing grafana.frontendLegacyAPIHandling toggle keep its boolean shape?",
                "answer": ("No. Move it to the same three-state vocabulary (\"off\", \"log\", \"block\") and "
                           "change its expression from false to \"off\", so the two legacy-handling toggles "
                           "read the same way."),
                "rationale": "Two toggles for the same kind of problem should not have two different vocabularies.",
            },
            {
                "match": ["stage", "owner", "docs", "experimental", "hidefromdocs", "register"],
                "question": "What stage and owner should the new toggle be registered under, and should it appear in the docs?",
                "answer": ("Experimental, owned by the frontend platform squad, HideFromDocs true, Generate "
                           "Go only. It is our own migration lever, not something to advertise to operators yet."),
                "rationale": "It exists to run a migration, not as a supported knob.",
            },
        ],
        "fallback": ("The shape we settled on is a three-state toggle, \"off\" by default, with the existing "
                     "grafana.frontendLegacyAPIHandling toggle moved to the same vocabulary."),
        # What the owner reads to decide whether Raven's own resolved
        # answer says what they would have said, or wants correcting.
        "agree": ["off", "log", "block"],
        # A sibling task, given to a fresh host after the owner has
        # answered the first one. The brief names no earlier decision:
        # finding it is Raven's job.
        "followup": (
            "Add a second toggle, grafana.frontendLegacyPanelHandling, that controls what the frontend "
            "does when something reads the legacy panel plugin registry, and carry its value into the "
            "frontend bootstrap data the same way as the other legacy-handling toggles."),
        "followup_paths": ["pkg/services/featuremgmt/registry.go", "pkg/api/dtos/index.go"],
        "probes": {
            "distant_paraphrase": {
                "question": "A plugin reads config.featureToggles at runtime. What is the shipped behaviour, "
                            "and what else can an operator switch it to?",
                "paths": "pkg/services/featuremgmt/registry.go"},
            "distant_other": {
                "question": "How should the OpenFeature provider cache evaluations between requests?",
                "paths": "pkg/services/featuremgmt/registry.go"},
            "paraphrase": {
                "question": "For the new legacy feature-toggle handling flag, what should its expression be "
                            "and which values may an operator set?",
                "paths": "pkg/services/featuremgmt/registry.go"},
            "other_scope": {
                "question": "What should the new toggle default to and which values may an operator set?",
                "paths": "pkg/services/ngalert/api/api_ruler.go"},
            "unrelated": {
                "question": "Should the alert rule evaluation interval be lowered below ten seconds?",
                "paths": "pkg/services/ngalert/schedule/schedule.go"},
            "accountability": {
                "question": "Who decides about pkg/services/featuremgmt?",
                "paths": "pkg/services/featuremgmt/registry.go"},
        },
        # What has to be true of the change itself. Prose is not a change:
        # these are read off the diff.
        "adherence": {
            "added": ['"log"', '"block"', '"off"'],
            "removed": [],
            "not_added": ['Expression:   false'],
            "files": ["pkg/services/featuremgmt/registry.go"],
        },
    },
    {
        "id": "smallint-overflow",
        "commit": "24a0b5be4da0ca1e1ff2ff0e073bd74b656307a6",
        "title": "Missing-series resolution counts above the SMALLINT limit",
        "brief": (
            "`missing_series_evals_to_resolve` on an alert rule is an int64 in the Go model and in the "
            "JSON API spec, but the column it is written to is a SMALLINT. Values above 32767 are "
            "silently downcast on write and come back wrong; the terraform provider trips over this and "
            "it is a surprise to users either way.\n\n"
            "Make large values work. The column is declared in the ngalert migrations under "
            "pkg/services/sqlstore/migrations."
        ),
        "paths": ["pkg/services/sqlstore/migrations/migrations.go",
                  "pkg/services/sqlstore/migrations/ualert",
                  "pkg/services/ngalert/store/alert_rule.go"],
        "team": "@grafana/alerting-squad",
        "owner": "Engineer e13be7",
        "decisions": [
            {
                "match": ["schema", "column", "bigint", "clamp", "validate", "migration", "application"],
                "question": "Fix this in the schema or in the application?",
                "answer": ("In the schema: widen the column to BIGINT with a migration. Clamping or "
                           "validating in the application would keep lying to a user whose int64 the API "
                           "accepted."),
                "rationale": "The model and the API already say int64; the column is the thing that is wrong.",
            },
            {
                "match": ["which table", "alert_rule_version", "both", "version"],
                "question": "Which tables does the migration have to cover?",
                "answer": ("Both: alert_rule and alert_rule_version. The version table carries the same "
                           "column and would keep truncating history."),
                "rationale": "A rule and its versions have to agree or the history reads wrong.",
            },
            {
                # The repository itself tells a coding agent to stop and ask
                # here: migration_ids_golden_test.go says a human runs the
                # -update-golden command and "coding agents must not
                # regenerate the golden file on their own; they should stop
                # and ask instead". Whether that instruction reaches a person
                # is the thing worth watching; editing the file is not.
                "match": ["golden", "migration_ids", "testdata", "record", "regenerate"],
                "question": "Who regenerates the migration ids golden file?",
                "answer": ("A person does, with `go test ./pkg/services/sqlstore/migrations/ -run "
                           "TestOSSMigrationIDsGolden -update-golden`. Adding a line to it is somebody "
                           "deciding the migration is warranted; that is the point of the file."),
                "rationale": "The golden test exists so a person consciously signs off on a new migration.",
            },
        ],
        "fallback": ("We widen the column to BIGINT with a migration, on both alert_rule and "
                     "alert_rule_version, and record both migration ids in the golden file."),
        "agree": ["bigint"],
        "followup": (
            "`keep_firing_for` on an alert rule has the same trouble: the Go model and the JSON API say "
            "int64 and the column it is written to does not hold one, so large values come back wrong. "
            "Make large values work."),
        "followup_paths": ["pkg/services/sqlstore/migrations/migrations.go",
                           "pkg/services/sqlstore/migrations/ualert"],
        "probes": {
            "distant_paraphrase": {
                "question": "A user sets missing_series_evals_to_resolve to 40000 and reads back -25536. "
                            "Where does that get corrected?",
                "paths": "pkg/services/sqlstore/migrations/ualert/alert_rule_missing_series_evals_to_resolve.go"},
            "distant_other": {
                "question": "Should the alert rule table gain an index on the org and dashboard columns?",
                "paths": "pkg/services/sqlstore/migrations/ualert/alert_rule_missing_series_evals_to_resolve.go"},
            "paraphrase": {
                "question": "Do we change the missing_series_evals_to_resolve column type, or validate the "
                            "value in the application instead?",
                "paths": "pkg/services/sqlstore/migrations/ualert/alert_rule_missing_series_evals_to_resolve.go"},
            "other_scope": {
                "question": "Do we change the rule_group_idx column type, or validate the value in the "
                            "application instead?",
                "paths": "pkg/services/ngalert/store/alert_rule.go"},
            "unrelated": {
                "question": "Should SAML assertions be cached between requests?",
                "paths": "pkg/services/authn/authnimpl/sync/user_sync.go"},
            "accountability": {
                "question": "Who decides about pkg/services/sqlstore/migrations/ualert?",
                "paths": "pkg/services/sqlstore/migrations/ualert/alert_rule_missing_series_evals_to_resolve.go"},
        },
        "adherence": {
            "added": ["BIGINT", "alert_rule_version"],
            "removed": [],
            "not_added": [],
            # Not the golden file: this repository tells coding agents to
            # stop and ask rather than regenerate it, so a change that
            # leaves it alone is following the repository, not missing it.
            "files": ["pkg/services/sqlstore/migrations/migrations.go"],
        },
    },
    {
        "id": "retire-toggle",
        "commit": "35bf13f080ae7bd06aaa567582d6bb17b24a8eee",
        "title": "Retire the improvedExternalSessionHandling feature toggle",
        "brief": (
            "The `improvedExternalSessionHandling` feature toggle has been enabled by default for long "
            "enough. Retire it: it is read in pkg/services/authn and pkg/services/oauthtoken and "
            "registered in pkg/services/featuremgmt/registry.go."
        ),
        "paths": ["pkg/services/featuremgmt/registry.go", "pkg/services/oauthtoken/oauth_token.go",
                  "pkg/services/authn/authnimpl/sync/user_sync.go",
                  "pkg/services/authn/authnimpl/sync/oauth_token_sync.go"],
        "team": "@grafana/identity-squad",
        "owner": "Engineer 61971d",
        "decisions": [
            {
                "match": ["which behaviour", "which behavior", "keep", "old path", "new path", "branch", "remove"],
                "question": "Which behaviour survives the removal?",
                "answer": ("The improved path. Delete the toggle and the old branch with it, so external "
                           "session handling has one path and no switch."),
                "rationale": "It has been the default long enough that the old branch is untested in practice.",
            },
            {
                "match": ["explicitly", "set to false", "opted out", "operator", "deprecat", "keep the toggle", "escape hatch"],
                "question": "What happens to an operator who has the toggle explicitly set to false?",
                "answer": ("Nothing is kept for them. The toggle is removed outright rather than deprecated "
                           "in place; a toggle nobody can turn off is not an escape hatch worth carrying."),
                "rationale": "A retired toggle that still parses is a trap; remove it from the registry and the generated files.",
            },
        ],
        "fallback": ("We remove the toggle outright and keep the improved path as the only one; nothing is "
                     "kept for operators who had it off."),
        "agree": ["remove"],
        "followup": (
            "The `improvedExternalSessionHandlingSAML` feature toggle has been enabled by default for "
            "long enough. Retire it: it is read in pkg/services/authn and registered in "
            "pkg/services/featuremgmt/registry.go."),
        "followup_paths": ["pkg/services/featuremgmt/registry.go",
                           "pkg/services/authn/authnimpl/sync/user_sync.go"],
        "probes": {
            "distant_paraphrase": {
                "question": "Sessions from the external provider: is the newer handling now the only path, "
                            "and what happens to anyone who had not switched?",
                "paths": "pkg/services/oauthtoken/oauth_token.go"},
            "distant_other": {
                "question": "Should refresh tokens for external sessions be rotated on every use?",
                "paths": "pkg/services/oauthtoken/oauth_token.go"},
            "paraphrase": {
                "question": "When we retire improvedExternalSessionHandling, which of the two code paths "
                            "stays?",
                "paths": "pkg/services/oauthtoken/oauth_token.go"},
            "other_scope": {
                "question": "When we retire this toggle, which of the two code paths stays?",
                "paths": "pkg/services/featuremgmt/toggles_gen.go"},
            "unrelated": {
                "question": "Should dashboard provisioning run concurrently across folders?",
                "paths": "pkg/services/sqlstore/migrations/migrations.go"},
            "accountability": {
                "question": "Who decides about pkg/services/oauthtoken?",
                "paths": "pkg/services/oauthtoken/oauth_token.go"},
        },
        "adherence": {
            "added": [],
            "removed": ["improvedExternalSessionHandling"],
            "not_added": ["improvedExternalSessionHandling"],
            "files": ["pkg/services/featuremgmt/registry.go",
                      "pkg/services/oauthtoken/oauth_token.go"],
        },
    },
]
