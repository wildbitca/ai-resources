# Domain detection

The domain selects the persona (`$AGENT_KIT/agents/personas/<role>-<domain>.md`) and the commands in `$AGENT_KIT/workflows/_domain-commands.yaml`, and fills `{{domain}}` in workflow prompts.

## Sources, in order

1. **Technologies** (or **Stack**) in `<workspace>/specs/PROJECT.md`.
2. The files being worked on (extensions, imports, config files).
3. The user's message — a task can target another stack than the app ("fix the Terraform module" → `devops` in a Flutter repo).

If the workspace has a single stack and the task does not mention another, use that stack. If several match, prefer the one the task touches; if still ambiguous, ask once.

## Signals

| Signal | Domain |
|--------|--------|
| flutter, dart, widget, riverpod, `lib/`, `pubspec.yaml` | `dart-flutter` |
| angular, component, `src/app/`, `angular.json` | `angular` |
| php, composer, `*.php` | `php` |
| symfony, doctrine, twig, `symfony.lock`, `config/packages/` | `symfony` |
| api platform, `#[ApiResource]`, state provider/processor | `api-platform` |
| terraform, crossplane, k8s, helm, `*.tf`, infra paths | `devops` |
| security audit, vulnerability, pen test, OWASP | `security` |

Domains without a persona use the base role only. Command inheritance: `api-platform` extends `symfony`, which extends `php`.
