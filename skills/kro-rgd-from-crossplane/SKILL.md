---
name: kro-rgd-from-crossplane
description: Create kro ResourceGraphDefinitions (RGDs) from Crossplane resources. Use when designing custom APIs that wrap Crossplane CRs, bundling related resources into unified RGDs (e.g. GitHubProject = teams + repos + team-repos), or when you need flexible schemas with defaults and parameter passing to sub-resources.
globs: "**/crossplane/**", "infra/**"
triggers: "kro, RGD, ResourceGraphDefinition, custom API, bundle resources, crossplane wrapper, kubernetes CRD, composite resource"
---

# kro RGD from Crossplane Resources

Design and implement kro ResourceGraphDefinitions that wrap Crossplane managed resources. Decide when to keep simple 1:1 RGDs vs. bundling into higher-level APIs.

## When to Use

- Designing a new custom API over Crossplane provider CRs
- Grouping related Crossplane resources into a single instance (e.g. project = root team + child teams + repos + team-repos)
- Parameterizing hardcoded values with schema defaults
- Passing project-level settings to child resources

## Design Decisions

### 1. Simple vs. Complex RGD

| Pattern           | Use when                                                         | Example                                                                                      |
|-------------------|------------------------------------------------------------------|----------------------------------------------------------------------------------------------|
| **1:1 RGD**       | One instance = one underlying CR; minimal wrapping               | GitHubRepository → Repository + TeamRepository                                               |
| **Composite RGD** | One instance = multiple CRs with shared lifecycle and references | GitHubProject → gate + rootTeam + childTeams + memberships + repositories + teamRepositories |

### 2. Bundling Heuristics

Bundle into a composite RGD when:

- Resources share a clear parent concept (e.g. "project", "environment")
- There are 1:N or N:M relationships (teams ↔ repos, parent team ↔ child teams)
- Common defaults apply to all children (providerConfigRef, deletionPolicy, visibility)
- Users think in terms of the parent (e.g. "create a project" not "create team, repos, team-repos")

Keep separate when:

- Resources are independent or reused across many contexts
- Single CR is sufficient and schema is trivial

### 3. Schema Design

- **Required fields**: Use `| required=true` for identifiers (name, etc.).
- **Optional with defaults**: Use `| default=value` for overridable settings.
- **Nested types**: Use `types:` for reusable structures (RepoSpec, ChildTeamSpec, PagesEntry).
- **Maps**: Use `map[string]Type` for dynamic children (childTeams, repositories).
- **Backward compatibility**: Avoid removing schema fields; add new fields as optional with defaults. Prefer a single RGD per CRD to avoid conflicting schemas.

### 4. Passing Values to Sub-Resources

- Top-level schema params → use `${schema.spec.?param.orValue(default)}` in templates.
- Project-level repository defaults → pass to all repository resources (e.g. `repositoryPrivate`, `repositoryVisibility`).
- Per-item overrides → use forEach + `schema.spec.repositories[repoSlug].?field.orValue(schema.spec.?repositoryField.orValue(default))`.

## RGD Structure

```yaml
spec:
  schema:
    group: <api-group>
    apiVersion: v1alpha1
    kind: <KindName>
    spec:
      name: string | required=true
      # Optional params with defaults
      providerConfigRef: string | default="default"
      deletionPolicy: string | default="Delete"
    types:
      NestedType:
        field: string
  resources:
    - id: resource1
      template: { ... }
    - id: resource2
      includeWhen: [ ${condition} ]
      forEach: [ { key: ${expr} } ]
      template: { ... }
```

## Patterns

### IncludeWhen

- Gate resources on schema fields: `includeWhen: [ ${schema.spec.rootTeamRef == "" && schema.spec.rootTeam != null} ]`.
- Conditional child resources: only create when map is non-empty.

### ForEach (independent iterators)

- kro forbids forEach that reference each other. Use a single flattened iterator:
    - Build `"key1|key2"` pairs, `.join(",")`, `.split(",")`, `.filter(p, p != "")`.
    - Parse with `pairKey.split("|")[0]` and `pairKey.split("|")[1]`.

### CEL

- Use `string()` for type coercion when needed (e.g. `"${string(...)}"`).
- Use `.orValue(default)` for optional schema fields.

## CRD and Versioning

- **One RGD per CRD**: Multiple RGDs with same group+kind can conflict; merge into one RGD to avoid "Property X was removed" errors.
- **New version**: Use different `kind` (e.g. GitHubProjectV2) for a new CRD, or add optional fields with defaults in place. Do not create duplicate RGDs that both define GitHubProject.

## Reference

- **Bundling patterns**: See `references/bundling-patterns.md` for flattened forEach, project defaults, rootTeamRef/rootTeam.
- kro schema: `https://kro.run/docs/concepts/simple-schema`
- CEL in templates: standard CEL syntax for expressions.
