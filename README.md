# ai-resources

Kit de Wildbit para agentes (Cursor, etc.): reglas, skills, workflows y el comando **`ai-resources`** (`generate`, `setup`). Aquí va lo que versionamos; **`~/.cursor`** es solo estado local del IDE.

## Instalar

Requisitos: **Homebrew**. Repo **`wildbitca/ai-resources`** es **privado** → hace falta un token de GitHub con lectura del código.

```bash
export HOMEBREW_GITHUB_API_TOKEN="ghp_TU_TOKEN"
brew tap wildbitca/ai-resources
brew install ai-resources
```

Comprueba: `ai-resources --help`

**Actualizar:** `brew update && brew upgrade ai-resources`

La fórmula está en **`Formula/ai-resources.rb`** (mismo repo que el código). Homebrew instala también **`python@3.12`** si no lo tienes.

## Uso

| Comando | Para qué |
|---------|----------|
| `ai-resources generate` | Regenerar skills e índice (`--help` para opciones). |
| `ai-resources setup` | MCP, IDE y workflows (`--help`). |

Tras instalar, **`AGENT_KIT`** apunta al kit en disco. Política y detalle: **`AGENTS.md`** · historial de versiones: **`CHANGELOG.md`**.
