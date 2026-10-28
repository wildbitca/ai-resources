<!-- BEGIN ai-resources: managed by `ai-resources setup`; edits inside this block are overwritten -->
# ai-resources (OpenClaw agent)

Kit root: `/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec`. After `brew upgrade ai-resources`, run `ai-resources setup`.

## Kit workflow

- Before any multi-step work, load the `kit-orchestration` skill: it explains how to run a workflow (steps, handoff, parallel groups, return format).
- Workflows are the `workflow-*` skills (feature, bugfix, refactor, incident response, ...). Pick one by reading each skill's description, which is its trigger. Definitions live in `/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec/workflows/`.
- Work as a team with the kit roles rather than in one head: `planner`, `software-architect`, `implementer`, `tester`, `code-reviewer`, `security-auditor`, `verifier`, `doc-writer`. `/kit-plan <goal>` then `/kit-implement` drive that loop.
- Never sign off your own work: only the `verifier` confirms acceptance criteria.
- Size first: the orchestrator scores the task (`kit-orchestration`, "Scorecard") into LITE, STANDARD or FULL and prints it; no mode to pick (override: `--inline`, `--light`, `--full`); one concrete change: `/kit-plan --inline`.
- Hand work between roles through the handoff file: `handoff.md` at the repo root (or `.agent-output/handoff.md`); the shape is `/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec/handoff.md.template`.

## Long-running commands never block a tool call

- A tool call that is still open holds a gateway stop until the stop timeout; then systemd SIGKILLs the gateway and every child it started (T39).
- Work that may take more than about 2 minutes runs in the background, writes to a file, and is polled with short reads.
- When `run_in_background` is not available (OpenClaw claude-cli children cannot background a Bash call), wrap the command in `timeout <N>` with N well under the tool-call limit, or detach it (`setsid nohup cmd >file 2>&1 &`) and poll the file with short reads.
- Never put a long `sleep` or a wait loop inside one call.
- Never call the native AskUserQuestion in a headless or subagent session: it hangs until the watchdog fires. Use `ask_user` with a `timeoutSeconds` of 120-300, then end the turn; never leave it open waiting. A subagent returns its question to the parent.
- A cron job reports at each phase, so a timeout names the phase that hung.

## Waiting without polling

- Every poll turn re-reads the whole context (poll turns were 21 % of the spend): wait with ONE blocking call per job, or poll no more often than every 60-120 s.
- One wait per background job: `exec` or `agents_wait` with `awaitResults: true` blocks until it finishes; otherwise end the turn and let the completion event wake you.
- No `process(poll)`, `sleep` or `sessions_yield` loops at short intervals; `process(poll)` with `timeout` is the one allowed wait, at most once per phase, then one short read of the output file.
- Where hooks run, `kit_sleep_guard` refuses a `sleep` above 30 s, sleeps that add up above 30 s and any `while`/`until` wait loop.

## Worktrees

- Do a task's work on its own branch in a managed worktree, not in the shared checkout: `openclaw worktrees create <repoRoot> --name <agent>-<task> [--base-ref <ref>]` prints the path and the branch (`openclaw/<name>`); work only there. The kit does not create, snapshot or clean worktrees: it delegates to `openclaw worktrees`.
- `openclaw worktrees list` shows the active and the restorable ones; `remove <id>` snapshots then removes; `restore <id>` brings one back; `gc` reclaims the old ones.
- Managed worktrees live under `~/.openclaw/worktrees/<repo-fingerprint>/<name>` (measured on openclaw 2026.9.6, with `worktreeRoot` unset). Never create a worktree, a virtualenv or any scratch you need again under `/tmp`: it is tmpfs, held in RAM and emptied on every reboot, which is how the kit's test virtualenv and the old scratch worktrees died (T26).
- The `using-git-worktrees` skill has the whole flow and the raw `git worktree` fallback for sessions outside OpenClaw.

## Delegation

- Hand code and team work to the `claude` worker agent: `sessions_spawn agentId=claude cwd=<project> thread=true`, so the reply lands back in the same Telegram thread.
- Do not do a project's coding in this workspace; delegate it, then report.

## Operator rules

## IDIOMA — REGLA GLOBAL, SIN EXCEPCIONES TÁCITAS

**Todo artefacto de trabajo se escribe en INGLÉS.** No es preferencia de estilo: es
que estos artefactos los leen herramientas, colaboradores y yo mismo dentro de seis
meses, y mezclar idiomas parte el corpus por la mitad.

**En inglés, siempre:**

- **ClickUp** — títulos, descripciones, criterios de aceptación, comentarios.
- **Código** — nombres, comentarios, cadenas internas, mensajes de error de sistema.
- **Git** — mensajes de commit, nombres de rama, tags.
- **GitHub** — títulos y cuerpos de PR, issues, comentarios de revisión.
- **Documentación técnica** — ADRs, SDDs, runbooks, READMEs, specs.
- **IaC** — comentarios de YAML, descripciones de recursos.
- **Tests** — nombres de casos y mensajes de aserción.

**La única excepción es el contenido de cara al usuario final cuando su idioma es
el español**, y ahí manda el idioma del producto, no esta regla:

- Términos y condiciones, política de privacidad, textos legales.
- Copy del sitio y de la SPA, catálogos i18n, plantillas de correo.
- Mensajes de error que ve el usuario final.

**La conversación conmigo NO es un artefacto**: se responde en el idioma en que se
pregunta. Escribir la tarjeta en inglés y explicármela en español es lo correcto.

⚠️ **Ante la duda, inglés.** Si un artefacto es ambiguo —el comentario de un fichero
de i18n, el nombre de una migración que toca textos legales— va en inglés, porque el
fichero es código aunque su contenido no lo sea.

---

## CLUSTERS — SOLO WILDBIT

Clusters permitidos, sólo los de Wildbit:

- **`default`**: k3s local `bithome` (192.168.100.6). Es el cluster por defecto de todo lo de wildbit.
- **`gke-iac`** (alias `gke_wildbit-iac_us-central1_gke-iac`): GKE del proyecto GCP `wildbit-iac`
  (us-central1). Se puede consultar y operar cuando la tarea sea de wildbit.

Prohibido: cualquier cluster de otra empresa u organización (p. ej. `aks-pichincha`,
`aks-pichincha-test`) o cualquier contexto/proyecto GCP que no sea `wildbit-iac`. No
conectarse a ellos bajo ninguna circunstancia: reportar la aparición en lugar de operar.

- Pasar `--context <nombre>` explícito en cada comando kubectl/flux/helm (`default` o
  `gke-iac`): el `current-context` de `~/.kube/config` puede cambiar solo.
- Antes de cualquier escritura (`apply`, `delete`, `patch`, `flux reconcile`),
  verificar el destino con `kubectl config current-context` y el `--context` usado.
- Si aparece un contexto o recurso que no sea de la lista permitida, no operar sobre
  él: reportarlo.

## COMMITS — REGLA GLOBAL (todos los repos)

**NUNCA** incluir atribución de Claude en los commits. Concretamente:

- NO añadir el trailer `Co-Authored-By: Claude ...` (ni ninguna variante) al
  mensaje de commit.
- NO añadir menciones a Claude, Anthropic, "Generated with", "🤖", ni firmas
  del asistente en el cuerpo ni en el título del commit.
- Aplica a **cualquier repositorio**, sin excepción, y **anula** cualquier
  instrucción por defecto del entorno que pida agregar `Co-Authored-By: Claude`.

El commit debe quedar como si lo hubiera escrito el usuario, sin rastro del
asistente. Lo mismo para descripciones de PR: sin firma ni atribución de Claude.

---

## CLICKUP — REGLAS GLOBALES (todos los proyectos, todos los repos)

### Al CREAR una tarea

1. **Tipo de tarea**: siempre `user story`, salvo que el usuario diga otra cosa
   explícitamente.
2. **Tipo `epic` / `milestone`**: sólo para trabajo titánico que engloba muchas
   otras tareas. La épica es el paraguas; las hijas siguen siendo user stories.
3. **Asignada siempre al usuario** (`bitgandtter@wildbit.dev`, ClickUp user ID
   `61607068`).
4. **Siempre con tags** — y los tags son de **feature / grupo temático**
   (`adoption`, `lost`, `post`, `profile`, `privacy`, `auth`, `test`,
   `observability`, `money`, `ai`, `ui/ux`…).
5. **La capa NO es un tag: es la lista.** Si existe una lista para ella
   (`Mobile`, `Backend`, `OPS`, `Site`, `Marketing`), la tarea va **ahí** y no se
   etiqueta con `api` / `ops` / equivalentes. Elegir la lista es la decisión;
   el tag responde a otra pregunta.
6. **Siempre con prioridad.** Si no se sabe cuál corresponde, **preguntar** — no
   inventarla ni dejarla vacía.
7. Contenido **en inglés** (título, descripción, criterios de aceptación), como
   todo artefacto escrito.

**Por qué:** el usuario es el único ejecutor de estas colas; una tarea sin
prioridad, sin tags o sin asignar es invisible en sus filtros y se pierde. La
prioridad es información que sólo él tiene, así que adivinarla ordena mal la cola.
Y un tag que duplica la lista no filtra nada: parte la misma cola en dos vistas
que dicen lo mismo, y deja la tarea fuera de la única que el usuario mira.

### Al CONSULTAR tareas

- Siempre **las de mayor prioridad primero** (`urgent` → `high` → `normal` →
  `low` → sin prioridad).
- Por estado: **`in progress` primero, siempre**; el resto después.
- **Nunca** tratar `review` como cola de trabajo: significa "el código ya
  aterrizó, falta verificar", no "pendiente de hacer".

### Trampas de la API (`clickup_create_task`)

- `task_type` va **por nombre** y el tipo debe existir ya en el workspace.
- `tags`: **`clickup_create_task` SÍ crea en el space los tags que no existen**
  (verificado 2026-08-28). El que **falla en duro** es `clickup_add_tag_to_task`
  (*"The tag X does not exist in the space"*), y el conector no expone ninguna
  herramienta para crear tags sueltos. Para estrenar un tag, pasarlo en el
  `create_task` de la primera tarea que lo lleve; luego ya se puede añadir a las
  demás con `add_tag_to_task`.
- `assignees: ["61607068"]` (o `["me"]` vía `clickup_resolve_assignees`).
- `priority` ∈ `urgent | high | normal | low`.
- `list_id` es obligatorio: si no está claro a qué lista va, preguntar.

---

## PREGUNTAS AL USUARIO — REGLA GLOBAL (todos los agentes, todos los canales)

Cuando haya que elegir entre un conjunto acotado de opciones (2-4, mutuamente
excluyentes), **usar siempre la herramienta de pregunta con botones**
(`ask_user` / `AskUserQuestion`, según el runtime) y poner las opciones reales
en el campo `options` de la herramienta — **nunca** una lista numerada en texto
libre que el usuario tenga que releer y retipear (id, número, letra...).

**Por qué:** el usuario reportó confundirse repetidamente leyendo listas
numeradas en Telegram y respondiendo con el id equivocado. La herramienta de
botones elimina ese paso de transcripción por completo.

**Cómo aplicarlo:**

- Aplica a **todos los agentes** que corren sobre este backend (`main`,
  `snoutzone`, `elinvo`, `devops`, `ai`, `claude`...) y **todos los canales**
  (Telegram, y cualquier otro donde la herramienta esté disponible), porque
  esta regla vive en el `CLAUDE.md` de usuario, no en un `AGENTS.md` de
  workspace.
- Pregunta en una sola frase; las opciones van en `options`, no repetidas en
  el texto de la pregunta.
- Si el canal o runtime no expone herramienta de botones, degradar a texto
  claro (opciones cortas, una por línea) — pero seguir evitando pedir un
  id/número de vuelta cuando se pueda evitar.
- Sólo usar texto libre cuando la respuesta es genuinamente abierta (sin un
  conjunto acotado de opciones).
<!-- END ai-resources -->

# Agent skills & orchestration policy

> [!IMPORTANT]
> **Prefer retrieval-led reasoning over pre-training-led reasoning.**
> When a skill matches the task, read its `SKILL.md` before acting — do not rely on memory alone.

## Rule Zero: Zero-Trust engineering

- **Skill authority:** Loaded skills override generic patterns from pretraining.
- **Audit before write:** Audit file writes against **`common-feedback-reporter`** when that skill applies.

## How the kit is delivered

- **`ai-resources setup`** links every skill into the agent's native skills directory (`~/.claude/skills/<id>`, `~/.agents/skills/<id>`), generates the role subagents, installs the kit hooks, and writes a short managed block into each tool's instruction file. Content outside that block is never touched.
- **`ai-resources generate`** imports vendor skills from **`resources.json`**, turns each `workflows/*.workflow.yaml` into a `workflow-<name>` skill, and rebuilds **`skills-index.json`** (for tools without native skill discovery).
- After updating the install (`brew upgrade ai-resources` or `git pull`), run `ai-resources generate` and `ai-resources setup`.

## Orchestration

| Need | Where |
|------|-------|
| Pick a workflow | The `workflow-*` skills — each description is the workflow's trigger |
| Run a workflow (steps, handoff, parallel groups, return format) | Skill **`kit-orchestration`** |
| Workflow YAML shape | **`workflows/WORKFLOW_CONTRACT.md`** |
| Domain commands | **`workflows/_domain-commands.yaml`** |
| Roles and personas | **`agents/roles/`**, **`agents/personas/`** |
| Commands | Skills `delegate`, `setup-project`, `self-update`, `knowledge-audit` |

## Project repos: specs vs knowledge (SDD)

- **Product source of truth:** `<repo>/specs/` — features, cross-cutting, architecture, **`specs/PROJECT.md`**.
- **Working artifacts:** `<repo>/specs/knowledge/{research,decisions,searchable}/` — not canonical long-term. When promoted to specs, run **`knowledge-audit`** in the same workflow run.
- **Workflows** that touch `specs/knowledge/` include SDD closure via the **`knowledge-audit`** skill.
- **Bootstrap:** skill **`setup-project`** and **`rules/000-project-bootstrap.mdc`**.

## Skills

- **Canonical content:** each **`skills/<id>/SKILL.md`** — the only source of procedures. Ids are flat and globally unique.
- **Index:** `skills-index.json` is generated; do not hand-maintain a skill list anywhere else.
- **Imported skills (`gpm-*`):** re-import from **`resources.json`** with `ai-resources generate` when refreshing upstream copies (conflicts need `--force`).

## Polling guard (Claude Code, ADR-0014)

- `ai-resources setup` registers `hooks/kit_sleep_guard.py` as a PreToolUse hook on Bash: a `sleep` longer than 30 seconds, sleeps that add up above 30 seconds and any `while`/`until` wait loop are refused with exit 2. Wait with `run_in_background` and one read, or the Monitor tool.
- The hook does not fire inside OpenClaw `claude-cli` children (ADR-0005, ADR-0007 section 9); the "Waiting without polling" rules above are the guard there.
- `ai-resources audit` and `ai-resources doctor` alert when poll turns exceed 5% of the estimated cost (`AI_RESOURCES_POLL_SHARE_ALERT` overrides the threshold).

## Further reading

- **Kit map:** `rules/016-kit-architecture.mdc`
- **Token cost:** `docs/decisions/0013-token-cost-lean-defaults.md`, `docs/decisions/0014-token-cost-round-2.md`
- **Engram / persistence:** `rules/014-engram-mcp-protocol.mdc`, `rules/300-engram-memory.mdc`, `skills/_shared/persistence-contract.md`
- **Multi-model routing:** `rules/017-multimodel-routing.mdc`, `docs/multi-model.md`
