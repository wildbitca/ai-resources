# Terraform → Crossplane Mapping Reference

## GitHub (Upbound)

| Terraform resource | Crossplane CR |
|--------------------|---------------|
| `team.github.upbound.io` (Team) | `team.team.github.upbound.io` |
| `repo.github.upbound.io` (Repository) | `repository.repo.github.upbound.io` |
| `user.github.upbound.io` (Membership) | `membership.user.github.upbound.io` |
| `teammembership.team.github.upbound.io` | TeamMembership |
| `teamrepository.team.github.upbound.io` | TeamRepository |

## Common HCL → spec mappings

- `name` → `forProvider.name`
- `description` → `forProvider.description`
- `private` → `forProvider.private`
- `visibility` → `forProvider.visibility`
- `team_id` / parent ref → `forProvider.teamIdRef.name` or `parentTeamReadSlug`
- `provider` block → `providerConfigRef.name`

## Sync order (ArgoCD)

1. ProviderConfig, Providers
2. Root resources (teams, VPCs)
3. Child resources (repos, subnets)
4. Bindings (team-repos, memberships)
