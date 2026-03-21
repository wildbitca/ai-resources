# kro RGD Bundling Patterns

## Flattened forEach (N:M relationships)

When you need `repoSlug × teamSlug` pairs and kro forbids nested/independent iterators:

```yaml
forEach:
  - pairKey: ${schema.spec.repositories.map(repoSlug, schema.spec.repositories[repoSlug].?teamPermissions.orValue({}).map(teamSlug, repoSlug + "|" + teamSlug).join(",")).join(",").split(",").filter(p, p != "")}
template:
  # ...
  name: ${pairKey.split("|")[1] + "-" + pairKey.split("|")[0]}
  permission: ${schema.spec.repositories[pairKey.split("|")[0]].teamPermissions[pairKey.split("|")[1]]}
```

## Project-level defaults → repositories

Pass schema params to all repo resources:

```yaml
private: ${schema.spec.?repositoryPrivate.orValue(true)}
visibility: ${schema.spec.?repositoryVisibility.orValue("private")}
```

## RootTeamRef vs rootTeam

- `rootTeamRef`: reference existing root team (string slug).
- `rootTeam`: inline root team spec (name, description, members).
- Use `includeWhen` to gate root team creation: `schema.spec.rootTeamRef == "" && schema.spec.rootTeam != null`.
