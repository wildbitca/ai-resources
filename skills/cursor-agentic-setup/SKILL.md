---
name: cursor-agentic-setup
description: Setup y actualización de Cursor para trabajo agentic — MCP, subagentes, workflows, handoff, flujo de implementación completo. Usa en proyectos nuevos o existentes para aplicar/actualizar buenas prácticas.
disable-model-invocation: true
---

# Cursor Agentic Setup — Skill

Configura o actualiza Cursor para trabajo agentic: MCP local, subagentes (planner, architect, implementer, tester, code-reviewer, security-auditor, verifier), workflows de 7 fases, handoff, análisis YYMMDD-NNN, y reglas de oro.

**Modos:** Setup (proyecto nuevo) | Update (proyecto existente — detecta faltantes y los añade)

**Para ejecutar:** Invoca este skill con `/cursor-agentic-setup` o pide "configurar cursor agentic", "setup agentic", "actualizar workflows de cursor". Funciona en cualquier proyecto (nuevo o existente).

---

## Handoff per workflow (anti-bottleneck)

**Problema:** Varios agents/workflows que usan un solo `handoff.md` generan cuello de botella y sobrescrituras.

**Solución:** Un archivo handoff por ejecución de workflow. Path: `.cursor/handoff-<scope>.md`.

- **Con worktree/branch:** scope = nombre de rama saneado (`/` → `-`), ej. `feature-auth` → `.cursor/handoff-feature-auth.md`
- **Sin worktree:** scope = nombre del workflow, ej. `.cursor/handoff-feature-implementation.md`

El **orquestador** debe inyectar `handoff_file` en el prompt de cada paso antes de invocar mcp_task. Los workflows usan `{{handoff_file}}` en los prompt_templates.

**Retrocompatibilidad:** Si no se inyecta `handoff_file`, usar `.cursor/handoff.md`.

---

## Paso 0: Determinar modo y tecnologías

### Modo

- **Setup:** No existe `.cursor/` o está vacío. Crear estructura completa.
- **Update:** Existe `.cursor/`. Comparar con canon y añadir/actualizar lo que falte.

### Descubrimiento (obligatorio)

Antes de crear archivos, pregunta y/o analiza el proyecto:

1. **Tipo:** mobile | web | backend | infra | multi
2. **Stack:** Flutter, Angular, React, Node, Python, etc.
3. **Servicios:** Supabase, Firebase, Sentry, Cloudflare, Figma, ClickUp, RevenueCat, etc.

Define variables de adaptación:

- `PROJECT_NAME` — Nombre del proyecto
- `DOMAIN` — mobile | web | infra
- `TEST_CMD` — `flutter test` | `npm test` | `pytest` | etc.
- `ANALYZE_CMD` — `dart analyze` | `eslint .` | etc.
- `DEPS_AUDIT_CMD` — `dart pub outdated` | `npm audit` | etc.
- `TEST_DIR` — `test/` | `__tests__/` | `tests/` | etc.

---

## Mapeo tecnología → MCP

| Tecnología | MCP(s) |
|------------|--------|
| Flutter | dart, maestro |
| Firebase | firebase |
| Supabase | supabase |
| Sentry | sentry |
| Cloudflare | cloudflare-docs, cloudflare-bindings, cloudflare-builds, cloudflare-observability |
| Figma | figma |
| ClickUp | clickup |
| RevenueCat | revenuecat |
| Web frontend | cursor-ide-browser |

**Regla:** Use only MCPs from this table for technologies confirmed in the project. Anything not listed here stays out of scope unless the user explicitly requests it.

---

## Estructura canónica

```
.cursor/
├── handoff.md.template
├── README.md
├── agents/
│   ├── planner.md
│   ├── implementer.md
│   ├── tester.md
│   ├── verifier.md
│   ├── software-architect.md
│   ├── code-reviewer.md
│   ├── security-auditor.md
│   ├── planner/analysis/.gitkeep
│   ├── tester/analysis/.gitkeep
│   ├── software-architect/analysis/.gitkeep
│   ├── code-reviewer/analysis/.gitkeep
│   └── security-auditor/analysis/.gitkeep
└── workflows/
    └── feature-implementation.workflow.yaml
```

---

## Update: checklist de detección

En modo Update, verificar y añadir solo lo que falte:

| Elemento | Existe | Acción |
|----------|--------|--------|
| handoff.md.template (con Implementation Workflow Fields; handoff per workflow: handoff-<scope>.md) | | Crear/actualizar |
| agents/planner.md (con YYMMDD-NNN y architect flow) | | Crear/actualizar |
| agents/implementer.md (con reglas: no tests, architect approval) | | Crear/actualizar |
| agents/tester.md (con analysis/, coverage) | | Crear/actualizar |
| agents/verifier.md | | Crear si falta |
| agents/software-architect.md | | Crear si falta |
| agents/code-reviewer.md | | Crear si falta |
| agents/security-auditor.md | | Crear si falta |
| agents/*/analysis/.gitkeep | | Crear carpetas si faltan |
| workflows/feature-implementation.workflow.yaml (7 fases) | | Crear/actualizar |
| mcp.json | | Crear según stack |
| .cursorrules | | Añadir referencias + Hack 2 (Kill Documentation Generation) |
| .gitignore | | Añadir `.cursor/agents/` (no versionar contenido de agents) |

---

## Flujo de implementación (7 fases)

```
plan → architect → implement → test → review → security → verify
```

Reglas de oro:
1. NUNCA implementar sin plan aprobado por architect
2. NUNCA el implementer escribe tests (lo hace tester)
3. SIEMPRE code-reviewer revisa antes de dar por terminado
4. SIEMPRE security-auditor para features con datos sensibles
5. Reportes en `YYMMDD-NNN.md` en cada `agents/<rol>/analysis/`
6. **`.cursor/agents/` no se versiona** — añadir a `.gitignore` en Setup y Update.

---

## Plantillas — ubicación

Las plantillas canónicas están en el mismo directorio que este skill, en `templates/`. Al ejecutar el skill:

- **Ruta típica:** `.cursor/skills/cursor-agentic-setup/templates/`
- **Copiar** cada archivo a `PROJECT/.cursor/` (o subcarpeta correspondiente)
- **Reemplazar** placeholders: {{PROJECT_NAME}}, {{TEST_CMD}}, {{DOMAIN}}, etc.

Estructura de templates:
```
templates/
├── handoff.md.template
├── feature-implementation.workflow.yaml   # Incluye reglas en comentarios
└── agents/
    ├── planner.md
    ├── implementer.md
    ├── tester.md
    ├── verifier.md
    ├── software-architect.md
    ├── code-reviewer.md
    └── security-auditor.md
```

---

## Plantillas (contenido de referencia)

El flujo y las reglas están en `feature-implementation.workflow.yaml` (comentarios al inicio del archivo). No crear workflow-implementation.md.

### handoff.md.template

Se copia a `handoff-<scope>.md` en cada ejecución. Scope = branch (saneado) o workflow name. Orquestador inyecta `handoff_file` en prompts.

```markdown
# Handoff

**Goal reached:** (1–2 sentences)

**Changes made:** (list file paths only, no code)
- 

**Unresolved / risks:**
- 

**Next assigned role:** (planner | implementer | tester | verifier | software-architect | code-reviewer | security-auditor | …)

**Domain:** (mobile | web | infra)

**Blocked:** (true | false)
**Return_to_step:** (plan | architect | implement | test | review | security)
**Block_reason:** (motivo breve)

---

## Implementation Workflow Fields

**Plan_ref:** (path to YYMMDD-NNN.md in .cursor/agents/planner/analysis/)
**Architect_approval:** (yes | no | pending)
**Test_report:** (path if applicable)
**Code_review_verdict:** (APPROVED | REQUIRES_CHANGES | pending)
**Security_audit:** (PASS | CONDITIONAL | FAIL | N/A)
**Requires_tests:** (yes | no)
**Security_critical_feature:** (yes | no)

---

*Copy to handoff-<scope>.md per workflow run. Orchestrator injects handoff_file. Flow and rules in .cursor/workflows/feature-implementation.workflow.yaml*
```

### agents/planner.md

YAML frontmatter: name, description, model: inherit. Contenido: planning specialist, crear plan en `.cursor/agents/planner/analysis/YYMMDD-NNN.md`, incluir Requires_tests y Security_critical_feature, handoff a software-architect. Plans deben ser validados por architect antes de implementar.

### agents/implementer.md

YAML frontmatter. Reglas: NUNCA implementar sin Architect_approval: yes; NUNCA escribir tests; SIEMPRE seguir plan en Plan_ref. Precondición: Architect_approval: yes.

### agents/tester.md

YAML frontmatter. Scope: solo escribir en TEST_DIR. Obligatorio: tests, reporte en tester/analysis/, coverage ≥80%. Usar {{TEST_CMD}} adaptado al proyecto.

### agents/verifier.md

YAML frontmatter. Validar acceptance criteria, ser escéptico, reportar qué pasó y qué falta.

### agents/software-architect.md

YAML frontmatter. Solo lectura. Validar plan contra arquitectura del proyecto. Generar análisis en software-architect/analysis/YYMMDD-NNN.md. Resultado: APROBADO o RECHAZADO. Adaptar "Arquitectura Base" y "Patrones" al stack del proyecto.

### agents/code-reviewer.md

YAML frontmatter. Solo lectura. Revisar código + tests. Generar reporte en code-reviewer/analysis/. Veredicto: APROBADO | REQUIRES_CHANGES. NO corrige código. Usar {{TEST_CMD}} y {{ANALYZE_CMD}}.

### agents/security-auditor.md

YAML frontmatter. Solo lectura. OWASP Top 10, ISO 27001. Generar reporte en security-auditor/analysis/. Resultado: PASS | CONDITIONAL | FAIL. Usar {{DEPS_AUDIT_CMD}}. Adaptar "Contexto Crítico" y comandos de verificación al stack.

### workflows/feature-implementation.workflow.yaml

7 steps: plan → architect → implement → test → review → security → verify. Cada step con subagent_type, handoff_to, on_concern_return_to, prompt_template. Usar `{{handoff_file}}` en todos los prompts; orquestador inyecta handoff_file antes de cada mcp_task. Adaptar domain, TEST_CMD en prompts (ej. "flutter test" vs "npm test").

---

## Paso 1: MCP

Crear `PROJECT/.cursor/mcp.json` con solo los MCPs del stack detectado. Copiar y fusionar desde las plantillas en `templates/`:
- `mcp-mobile.json` — para Flutter/mobile
- `mcp-web.json` — para web
- `mcp-plugin-servers.json` — supabase, sentry, figma, cloudflare (fusionar solo los confirmados)

---

## Paso 2–6 (Setup) o solo faltantes (Update)

Crear/actualizar en orden:
1. handoff.md.template
2. agents/*.md (7 agentes)
3. agents/*/analysis/.gitkeep
4. workflows/feature-implementation.workflow.yaml (flujo + reglas en comentarios)
5. .cursorrules — referencia a handoff y .cursor/workflows/
6. **.gitignore** — añadir `.cursor/agents/` para no versionar salidas y análisis de agents (solo local)

---

## .cursorrules mínimo

Incluir:
- Dominio (mobile | web | infra)
- MCPs permitidos
- "Complex tasks: consult .cursor/workflows/; handoff per workflow (handoff-<scope>.md)"
- **"Documentation (Hack 2 — Kill the Documentation Generation):"** Do NOT proactively create or update README, CHANGELOG, or other documentation files. Only add or change docs when the user explicitly asks (e.g. "add a README", "update the changelog"). Prefer minimal inline comments over new .md files. Apply this as much as possible.

---

## Referencias

- [Cursor MCP](https://cursor.com/help/customization/mcp)
- [Cursor Subagents](https://cursor.com/docs/subagents)
- [Cursor Worktrees](https://cursor.com/docs/configuration/worktrees)
- [Working with agents](https://cursor.com/learn/working-with-agents)
