# ai-resources

Kit de recursos para agentes de AI (Cursor, etc.): reglas, skills, workflows y el comando **`ai-resources`** (`generate`, `setup`). Aquí va lo que versionamos; **`~/.cursor`** es solo estado local del IDE.

## Instalar

Requisito: **Homebrew**.

Homebrew, si no le pasas URL, resuelve `brew tap usuario/nombre` como **`github.com/usuario/homebrew-nombre`**, no como `usuario/nombre`. Por eso hay que indicar la URL del repo real:

```bash
brew tap wildbitca/ai-resources https://github.com/wildbitca/ai-resources.git
brew install ai-resources
```

Comprueba: `ai-resources --help`

**Actualizar:** `brew update && brew upgrade ai-resources`

La fórmula está en **`Formula/ai-resources.rb`**. Declara **`python@3.12`**; el binario del kit suele ser **`python3.12`** (no siempre `python3`). Si `ai-resources --help` falla, prueba `brew reinstall python@3.12` y vuelve a instalar el kit.

## Uso

| Comando | Para qué |
|---------|----------|
| `ai-resources generate` | Regenerar skills e índice (`--help` para opciones). |
| `ai-resources setup` | MCP, IDE y workflows (`--help`). |

Tras instalar, **`AGENT_KIT`** apunta al kit en disco. Política y detalle: **`AGENTS.md`** · historial de versiones: **`CHANGELOG.md`**.
